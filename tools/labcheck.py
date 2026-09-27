#!/usr/bin/env python3
"""Static checker for a GNS3 lab built from Cisco IOS routers and VPCS hosts.

It reads the .gns3 topology plus each node's startup config and checks what a
reviewer would otherwise verify by hand:

  * both ends of every link sit in the same IP subnet (switches are followed,
    so a LAN made of router + switch + PCs counts as one segment)
  * no duplicate addresses; no overlapping subnets on one router
  * every routed interface is advertised by OSPF, every `network` statement
    matches something, and wildcards aren't wider than the interface subnet
  * router-to-router links run OSPF on both ends, in the same area
  * each host's default gateway is a router address on the host's own segment
  * NAT (if configured): outside interface exists, ACL covers the inside LAN

Standard library only.  Exit status is 1 if any ERROR was found.

    python tools/labcheck.py gns3/project.gns3
    python tools/labcheck.py gns3/project.gns3 --configs reference-configs
    python tools/labcheck.py gns3/project.gns3 --table          # addressing plan (Markdown)
    python tools/labcheck.py gns3/project.gns3 --suggest-p2p 10.0.0.0/27
    python tools/labcheck.py gns3/project.gns3 --svg docs/topology.svg
"""

from __future__ import annotations

import argparse
import html
import ipaddress as ip
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PREFIXES = {
    "f": "FastEthernet",
    "fa": "FastEthernet",
    "g": "GigabitEthernet",
    "gi": "GigabitEthernet",
    "s": "Serial",
    "se": "Serial",
    "e": "Ethernet",
    "eth": "Ethernet",
}
ROUTER_TYPES = {"dynamips", "iou", "qemu"}
L2_TYPES = {"ethernet_switch", "ethernet_hub"}


# --------------------------------------------------------------------------- model


@dataclass
class Interface:
    name: str
    addr: ip.IPv4Interface | None = None
    dhcp: bool = False
    shutdown: bool = False
    nat: str | None = None  # "inside" | "outside"
    description: str = ""


@dataclass
class Network:
    net: ip.IPv4Address
    wildcard: ip.IPv4Address
    area: str

    def matches(self, addr: ip.IPv4Address) -> bool:
        mask = ~int(self.wildcard) & 0xFFFFFFFF
        return int(addr) & mask == int(self.net) & mask

    @property
    def prefixlen(self) -> int | None:
        w = int(self.wildcard)
        return 32 - w.bit_length() if w & (w + 1) == 0 else None

    def __str__(self) -> str:
        return f"network {self.net} {self.wildcard} area {self.area}"


@dataclass
class Device:
    name: str
    kind: str  # router | host | other
    interfaces: dict[str, Interface] = field(default_factory=dict)
    ospf: dict[str, list[Network]] = field(default_factory=dict)
    passive: set[str] = field(default_factory=set)
    gateway: ip.IPv4Address | None = None
    nat_rules: list[tuple[str, str]] = field(default_factory=list)  # (acl, iface)
    acls: dict[str, list[Network]] = field(default_factory=dict)
    dhcp_pools: list[ip.IPv4Network] = field(default_factory=list)
    configured: bool = False
    x: float = 0
    y: float = 0


# ------------------------------------------------------------------------- parsing


def expand(label: str) -> str:
    m = re.fullmatch(r"([A-Za-z]+)\s*(\d+(?:/\d+)*)", label.strip())
    if not m:
        return label
    return PREFIXES.get(m[1].lower(), m[1]) + m[2]


def parse_ios(text: str, dev: Device) -> None:
    section, current = None, None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.strip().startswith("!"):
            section = current = None
            continue
        if not line.startswith(" "):
            section, current = None, None
            words = line.split()
            if words[0] == "hostname":
                dev.name = words[1]
            elif words[0] == "interface":
                section, current = "if", dev.interfaces.setdefault(
                    expand(words[1]), Interface(expand(words[1]))
                )
            elif words[:2] == ["router", "ospf"]:
                section, current = "ospf", dev.ospf.setdefault(words[2], [])
            elif words[:3] == ["ip", "dhcp", "pool"]:
                section = "dhcp"
            elif words[0] == "access-list" and len(words) >= 4 and words[2] == "permit":
                wc = words[4] if len(words) > 4 else "0.0.0.0"
                dev.acls.setdefault(words[1], []).append(
                    Network(ip.IPv4Address(words[3]), ip.IPv4Address(wc), "-")
                )
            elif line.startswith("ip nat inside source list") and "interface" in words:
                dev.nat_rules.append((words[5], expand(words[words.index("interface") + 1])))
            continue

        words = line.split()
        if section == "if":
            dev.configured = True
            if words[:2] == ["ip", "address"] and words[2] == "dhcp":
                current.dhcp = True
            elif words[:2] == ["ip", "address"] and len(words) >= 4:
                current.addr = ip.IPv4Interface(f"{words[2]}/{words[3]}")
            elif words == ["shutdown"]:
                current.shutdown = True
            elif words[:2] == ["ip", "nat"]:
                current.nat = words[2]
            elif words[0] == "description":
                current.description = " ".join(words[1:])
        elif section == "ospf":
            if words[0] == "network":
                current.append(Network(ip.IPv4Address(words[1]), ip.IPv4Address(words[2]), words[4]))
            elif words[0] == "passive-interface":
                dev.passive.add(expand(words[1]))
        elif section == "dhcp" and words[0] == "network":
            dev.dhcp_pools.append(ip.IPv4Network(f"{words[1]}/{words[2]}", strict=False))


def parse_vpcs(text: str, dev: Device) -> None:
    iface = dev.interfaces.setdefault("Ethernet0", Interface("Ethernet0"))
    for line in text.splitlines():
        words = line.split()
        if words[:2] == ["ip", "dhcp"]:
            iface.dhcp = dev.configured = True
        elif words[:1] == ["ip"] and len(words) >= 2:
            addr = words[1] if "/" in words[1] else f"{words[1]}/{words[3] if len(words) > 3 else 24}"
            iface.addr = ip.IPv4Interface(addr)
            if len(words) > 2 and "." in words[2]:
                dev.gateway = ip.IPv4Address(words[2])
            dev.configured = True


# ------------------------------------------------------------------------- project


@dataclass
class Lab:
    devices: dict[str, Device]
    links: list[tuple[tuple[str, str], tuple[str, str]]]
    drawings: list[dict]
    node_types: dict[str, str]


def load(project: Path, config_dir: Path | None) -> Lab:
    data = json.loads(project.read_text())
    topo = data["topology"]
    files = project.parent / "project-files"
    devices, by_id, types = {}, {}, {}

    for node in topo["nodes"]:
        name, ntype = node["name"], node["node_type"]
        kind = "router" if ntype in ROUTER_TYPES else "host" if ntype == "vpcs" else "other"
        dev = Device(name, kind, x=node["x"] + node.get("width", 60) / 2, y=node["y"] + node.get("height", 45) / 2)
        types[name] = ntype
        if ntype == "dynamips":
            override = config_dir and config_dir / f"{name}.cfg"
            path = (
                override
                if override and override.exists()
                else files / "dynamips" / node["node_id"] / "configs" / f"i{node['properties']['dynamips_id']}_startup-config.cfg"
            )
            if path.exists():
                parse_ios(path.read_text(errors="replace"), dev)
                dev.name = name  # keep the GNS3 label as the identity
        elif ntype == "vpcs":
            override = config_dir and config_dir / f"{name}.vpc"
            path = override if override and override.exists() else files / "vpcs" / node["node_id"] / "startup.vpc"
            if path.exists():
                parse_vpcs(path.read_text(errors="replace"), dev)
        devices[name] = dev
        by_id[node["node_id"]] = name

    links = []
    for link in topo["links"]:
        ends = []
        for end in link["nodes"]:
            label = (end.get("label") or {}).get("text") or f"{end['adapter_number']}/{end['port_number']}"
            ends.append((by_id[end["node_id"]], expand(label)))
        links.append(tuple(ends))
    return Lab(devices, links, topo.get("drawings", []), types)


def segments(lab: Lab) -> list[set[tuple[str, str]]]:
    """Group link endpoints into L2 broadcast domains (switches are transparent)."""
    parent: dict = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        parent[find(a)] = find(b)

    for a, b in lab.links:
        union(a, b)
        for end in (a, b):
            if lab.node_types[end[0]] in L2_TYPES:
                union(end, ("switch", end[0]))
    groups: dict = {}
    for k in list(parent):
        if k[0] != "switch" and lab.node_types.get(k[0]) not in L2_TYPES:
            groups.setdefault(find(k), set()).add(k)
    return list(groups.values())


# -------------------------------------------------------------------------- checks


class Report:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, level: str, where: str, msg: str):
        self.items.append((level, where, msg))

    @property
    def errors(self):
        return [i for i in self.items if i[0] == "ERROR"]


def check(lab: Lab) -> Report:
    r = Report()
    routers = [d for d in lab.devices.values() if d.kind == "router"]
    linked = {end for link in lab.links for end in link}

    for dev in routers:
        if not dev.configured:
            r.add("WARN", dev.name, "router has no startup configuration (boots blank)")
            continue
        up = {n: i for n, i in dev.interfaces.items() if i.addr and not i.shutdown}
        # several interfaces of one router in the same subnet
        by_net: dict = {}
        for name, iface in sorted(up.items()):
            by_net.setdefault(iface.addr.network, []).append(name)
        for net, names in by_net.items():
            if len(names) > 1:
                r.add("WARN", dev.name, f"{', '.join(names)} share {net}; "
                      "give each point-to-point link its own /30 or /31")
        nets = sorted(by_net, key=lambda n: (int(n.network_address), n.prefixlen))
        for i, a in enumerate(nets):
            for b in nets[i + 1 :]:
                if a.overlaps(b):
                    r.add("ERROR", dev.name, f"interface subnets {a} and {b} overlap")
        # OSPF coverage
        statements = [(pid, n) for pid, nets in dev.ospf.items() for n in nets]
        for pid, nets in dev.ospf.items():
            if not nets:
                r.add("WARN", dev.name, f"`router ospf {pid}` has no network statements (leftover process)")
        for name, iface in up.items():
            if iface.nat == "outside":
                continue
            hits = [(pid, n) for pid, n in statements if n.matches(iface.addr.ip)]
            if not hits:
                r.add("ERROR", dev.name, f"{name} {iface.addr} is not advertised by OSPF")
            for pid, n in hits:
                if n.prefixlen is not None and n.prefixlen < iface.addr.network.prefixlen:
                    r.add("WARN", dev.name, f"`{n}` (/{n.prefixlen}) is wider than {name}'s subnet "
                          f"{iface.addr.network}; match the interface mask")
        for pid, n in statements:
            if not any(n.matches(i.addr.ip) for i in up.values()):
                r.add("WARN", dev.name, f"`{n}` (ospf {pid}) matches no interface")
        for name, iface in dev.interfaces.items():
            if (dev.name, name) in linked and iface.shutdown:
                r.add("WARN", dev.name, f"{name} is cabled in the topology but shut down")
        # NAT
        for acl, out in dev.nat_rules:
            outside = dev.interfaces.get(out)
            if not outside or outside.nat != "outside":
                r.add("ERROR", dev.name, f"NAT overloads onto {out}, which is not `ip nat outside`")
            inside = [i for i in dev.interfaces.values() if i.nat == "inside" and i.addr]
            if not inside:
                r.add("ERROR", dev.name, "NAT rule present but no `ip nat inside` interface")
            for i in inside:
                if not any(n.matches(i.addr.network.network_address) for n in dev.acls.get(acl, [])):
                    r.add("ERROR", dev.name, f"access-list {acl} does not permit inside LAN {i.addr.network}")

    # duplicate addresses
    seen: dict = {}
    for dev in lab.devices.values():
        for iface in dev.interfaces.values():
            if iface.addr:
                other = seen.setdefault(iface.addr.ip, (dev.name, iface.name))
                if other != (dev.name, iface.name):
                    r.add("ERROR", dev.name, f"{iface.addr.ip} on {iface.name} is also used on {other[0]} {other[1]}")

    # per-segment addressing
    for seg in segments(lab):
        members = []
        for name, ifname in sorted(seg):
            dev = lab.devices[name]
            iface = dev.interfaces.get(ifname)
            if iface is None and dev.kind == "host":
                iface = dev.interfaces.get("Ethernet0")
            members.append((dev, ifname, iface))
        static = [(d, n, i) for d, n, i in members if i and i.addr and not i.shutdown]
        label = ", ".join(f"{d.name}:{n}" for d, n, _ in members)
        nets = {i.addr.network for _, _, i in static}
        if len(nets) > 1:
            detail = "; ".join(f"{d.name} {n} {i.addr}" for d, n, i in static)
            r.add("ERROR", label, f"link/segment mixes subnets: {detail}")
        routers_here = [(d, n, i) for d, n, i in static if d.kind == "router"]
        if len(routers_here) >= 2:
            areas = set()
            for d, n, i in routers_here:
                hit = [x for nets_ in d.ospf.values() for x in nets_ if x.matches(i.addr.ip)]
                if not hit:
                    r.add("ERROR", label, f"{d.name} {n} does not run OSPF, so no adjacency forms here")
                areas |= {x.area for x in hit[:1]}
                if n in d.passive:
                    r.add("ERROR", label, f"{d.name} {n} is passive, so no adjacency forms here")
            if len(areas) > 1:
                r.add("ERROR", label, f"OSPF area mismatch across the link: {sorted(areas)}")
        for d, n, i in members:
            if d.kind != "host" or not d.configured:
                continue
            gw_ifaces = [x for x in routers_here if x[2].addr.ip == d.gateway]
            dhcp_ifaces = [x for x in routers_here if any(x[2].addr.network == p for p in x[0].dhcp_pools)]
            if i and i.dhcp:
                if not dhcp_ifaces:
                    r.add("ERROR", d.name, "uses DHCP but no router on its segment has a matching `ip dhcp pool`")
            elif d.gateway is None:
                r.add("WARN", d.name, "host has an address but no default gateway")
            elif not gw_ifaces:
                r.add("ERROR", d.name, f"gateway {d.gateway} is not a router address on its segment")
            elif i and i.addr and i.addr.network != gw_ifaces[0][2].addr.network:
                r.add("ERROR", d.name, f"{i.addr} is not in the gateway's subnet {gw_ifaces[0][2].addr.network}")
    return r


# ------------------------------------------------------------------------- outputs


def addressing_table(lab: Lab) -> str:
    rows = ["| Device | Interface | Address | Subnet | Peer | OSPF |", "|---|---|---|---|---|---|"]
    peers = {}
    for a, b in lab.links:
        peers[a], peers[b] = b, a
    for dev in sorted(lab.devices.values(), key=lambda d: (d.kind != "router", d.name)):
        if dev.kind not in ("router", "host"):
            continue
        for name, iface in sorted(dev.interfaces.items()):
            if not (iface.addr or iface.dhcp) or iface.shutdown:
                continue
            peer = peers.get((dev.name, name)) or (peers.get((dev.name, "Ethernet0")) if dev.kind == "host" else None)
            ospf = ""
            if iface.addr:
                hit = [(pid, n) for pid, nets in dev.ospf.items() for n in nets if n.matches(iface.addr.ip)]
                ospf = f"{hit[0][0]} / area {hit[0][1].area}" if hit else ""
            addr = "DHCP" if iface.dhcp else str(iface.addr.ip)
            subnet = str(iface.addr.network) if iface.addr else ""
            if dev.kind == "host" and dev.gateway:
                ospf = f"gw {dev.gateway}"
            rows.append(f"| {dev.name} | {name} | {addr} | {subnet} | {f'{peer[0]} {peer[1]}' if peer else ''} | {ospf} |")
    return "\n".join(rows)


def suggest_p2p(lab: Lab, block: str) -> str:
    pool = ip.ip_network(block).subnets(new_prefix=30)
    rows = ["| Link | Subnet | A | B |", "|---|---|---|---|"]
    for a, b in lab.links:
        if lab.devices[a[0]].kind == lab.devices[b[0]].kind == "router":
            try:
                net = next(pool)
            except StopIteration:
                sys.exit(f"{block} is too small for one /30 per router link")
            h = list(net.hosts())
            rows.append(f"| {a[0]} {a[1]} ↔ {b[0]} {b[1]} | {net} | {a[0]} {h[0]} | {b[0]} {h[1]} |")
    return "\n".join(rows)


def svg(lab: Lab) -> str:
    xs = [d.x for d in lab.devices.values()]
    ys = [d.y for d in lab.devices.values()]
    pad = 90
    x0, y0 = min(xs) - pad, min(ys) - pad
    w, h = max(xs) - x0 + pad, max(ys) - y0 + pad
    colors = {"router": "#1f6feb", "host": "#8250df", "other": "#6e7781"}
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.0f} {y0:.0f} {w:.0f} {h:.0f}" '
        f'font-family="ui-monospace,Menlo,Consolas,monospace" font-size="13">',
        f'<rect x="{x0:.0f}" y="{y0:.0f}" width="{w:.0f}" height="{h:.0f}" fill="#ffffff"/>',
    ]
    for a, b in lab.links:
        da, db = lab.devices[a[0]], lab.devices[b[0]]
        serial = a[1].startswith("Serial")
        out.append(
            f'<line x1="{da.x:.0f}" y1="{da.y:.0f}" x2="{db.x:.0f}" y2="{db.y:.0f}" '
            f'stroke="{"#cf222e" if serial else "#57606a"}" stroke-width="{2.5 if serial else 1.5}"/>'
        )
        for d, other, (_, ifname) in ((da, db, a), (db, da, b)):
            iface = d.interfaces.get(ifname) or (d.interfaces.get("Ethernet0") if d.kind == "host" else None)
            text = ifname.replace("FastEthernet", "f").replace("Serial", "s").replace("Ethernet", "e")
            if iface and iface.addr:
                text += f" .{str(iface.addr.ip).rsplit('.', 1)[1]}"
            elif iface and iface.dhcp:
                text += " dhcp"
            lx, ly = d.x + (other.x - d.x) * 0.28, d.y + (other.y - d.y) * 0.28
            out.append(
                f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="middle" fill="#24292f" font-size="11" '
                f'stroke="#fff" stroke-width="3" paint-order="stroke">{html.escape(text)}</text>'
            )
    for d in lab.devices.values():
        kind = "other" if lab.node_types[d.name] in L2_TYPES | {"nat", "docker", "cloud"} else d.kind
        shape = (
            f'<circle cx="{d.x:.0f}" cy="{d.y:.0f}" r="20" fill="{colors[kind]}"/>'
            if kind == "router"
            else f'<rect x="{d.x - 18:.0f}" y="{d.y - 14:.0f}" width="36" height="28" rx="5" fill="{colors[kind]}"/>'
        )
        out.append(shape)
        out.append(
            f'<text x="{d.x:.0f}" y="{d.y + 36:.0f}" text-anchor="middle" font-weight="bold" fill="#24292f">'
            f"{html.escape(d.name)}</text>"
        )
    for dr in lab.drawings:
        for text in re.findall(r">([^<]+)<", dr.get("svg", "")):
            if "/" in text:  # subnet annotations only
                for i, part in enumerate(text.split("\n")):
                    out.append(
                        f'<text x="{dr["x"]}" y="{dr["y"] + 14 + i * 16}" fill="#1a7f37" font-weight="bold">'
                        f"{html.escape(part)}</text>"
                    )
    legend = [("router", "circle", "router"), ("host", "rect", "host (VPCS)"), ("other", "rect", "switch / NAT / container")]
    lx, ly = x0 + 20, y0 + 24
    for i, (kind, shape, text) in enumerate(legend):
        y = ly + i * 22
        out.append(
            f'<circle cx="{lx + 8:.0f}" cy="{y:.0f}" r="7" fill="{colors[kind]}"/>'
            if shape == "circle"
            else f'<rect x="{lx:.0f}" y="{y - 7:.0f}" width="16" height="14" rx="3" fill="{colors[kind]}"/>'
        )
        out.append(f'<text x="{lx + 24:.0f}" y="{y + 4:.0f}" fill="#24292f">{text}</text>')
    y = ly + len(legend) * 22
    out.append(f'<line x1="{lx:.0f}" y1="{y:.0f}" x2="{lx + 16:.0f}" y2="{y:.0f}" stroke="#cf222e" stroke-width="2.5"/>')
    out.append(f'<text x="{lx + 24:.0f}" y="{y + 4:.0f}" fill="#24292f">serial link</text>')
    out.append("</svg>")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("project", type=Path, help="path to the .gns3 file")
    p.add_argument("--configs", type=Path, help="directory of <NODE>.cfg / <NODE>.vpc files that override project-files")
    p.add_argument("--table", action="store_true", help="print the addressing plan as a Markdown table")
    p.add_argument("--suggest-p2p", metavar="CIDR", help="propose one /30 per router-to-router link from CIDR")
    p.add_argument("--svg", type=Path, help="write a topology diagram")
    args = p.parse_args(argv)

    lab = load(args.project, args.configs)
    if args.table:
        print(addressing_table(lab))
        return 0
    if args.suggest_p2p:
        print(suggest_p2p(lab, args.suggest_p2p))
        return 0
    if args.svg:
        args.svg.write_text(svg(lab))
        print(f"wrote {args.svg}")
        return 0

    report = check(lab)
    routers = sum(d.kind == "router" for d in lab.devices.values())
    hosts = sum(d.kind == "host" for d in lab.devices.values())
    print(f"{args.project.name}: {routers} routers, {hosts} hosts, {len(lab.links)} links, {len(segments(lab))} segments\n")
    for level, where, msg in sorted(report.items, key=lambda i: (i[0] != "ERROR", i[1])):
        print(f"{level:5}  {where}: {msg}")
    errors, warns = len(report.errors), len(report.items) - len(report.errors)
    print(f"\n{errors} error(s), {warns} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
