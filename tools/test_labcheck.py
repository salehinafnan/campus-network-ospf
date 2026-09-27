"""Unit tests for labcheck.py:  python -m unittest discover tools"""

import json
import tempfile
import unittest
from pathlib import Path

import labcheck

ROUTER = """hostname {name}
!
interface FastEthernet0/0
 ip address {lan} 255.255.255.0
!
interface Serial1/0
 ip address {wan} 255.255.255.252
!
router ospf 1
{networks}
!
end
"""


def build(tmp: Path, r1_networks: str, r2_networks: str, pc_line: str) -> Path:
    nodes = [
        {"name": "R1", "node_type": "dynamips", "node_id": "r1", "x": 0, "y": 0, "properties": {"dynamips_id": 1}},
        {"name": "R2", "node_type": "dynamips", "node_id": "r2", "x": 100, "y": 0, "properties": {"dynamips_id": 2}},
        {"name": "SW", "node_type": "ethernet_switch", "node_id": "sw", "x": 0, "y": 100, "properties": {}},
        {"name": "PC", "node_type": "vpcs", "node_id": "pc", "x": 0, "y": 200, "properties": {}},
    ]
    end = lambda node, label: {"node_id": node, "adapter_number": 0, "port_number": 0, "label": {"text": label}}
    links = [
        {"nodes": [end("r1", "s1/0"), end("r2", "s1/0")]},
        {"nodes": [end("r1", "f0/0"), end("sw", "e0")]},
        {"nodes": [end("sw", "e1"), end("pc", "e0")]},
    ]
    for node, name, lan, wan, nets in (
        ("r1", "R1", "10.1.1.1", "10.0.0.1", r1_networks),
        ("r2", "R2", "10.2.2.1", "10.0.0.2", r2_networks),
    ):
        d = tmp / "project-files" / "dynamips" / node / "configs"
        d.mkdir(parents=True)
        (d / f"i{node[1]}_startup-config.cfg").write_text(ROUTER.format(name=name, lan=lan, wan=wan, networks=nets))
    (tmp / "project-files" / "vpcs" / "pc").mkdir(parents=True)
    (tmp / "project-files" / "vpcs" / "pc" / "startup.vpc").write_text(f"set pcname PC\n{pc_line}\n")
    project = tmp / "lab.gns3"
    project.write_text(json.dumps({"topology": {"nodes": nodes, "links": links, "drawings": []}}))
    return project


GOOD_R1 = " network 10.1.1.0 0.0.0.255 area 0\n network 10.0.0.0 0.0.0.3 area 0"
GOOD_R2 = " network 10.2.2.0 0.0.0.255 area 0\n network 10.0.0.0 0.0.0.3 area 0"


class LabcheckTest(unittest.TestCase):
    def run_check(self, r1=GOOD_R1, r2=GOOD_R2, pc="ip 10.1.1.10 10.1.1.1 24"):
        with tempfile.TemporaryDirectory() as tmp:
            lab = labcheck.load(build(Path(tmp), r1, r2, pc), None)
            return [(lvl, msg) for lvl, _, msg in labcheck.check(lab).items]

    def test_clean_lab_passes(self):
        self.assertEqual(self.run_check(), [])

    def test_unadvertised_lan_is_an_error(self):
        items = self.run_check(r2=" network 10.0.0.0 0.0.0.3 area 0")
        self.assertIn(("ERROR", "FastEthernet0/0 10.2.2.1/24 is not advertised by OSPF"), items)

    def test_area_mismatch_is_an_error(self):
        items = self.run_check(r2=" network 10.2.2.0 0.0.0.255 area 0\n network 10.0.0.0 0.0.0.3 area 1")
        self.assertTrue(any(lvl == "ERROR" and "area mismatch" in msg for lvl, msg in items))

    def test_wide_wildcard_is_a_warning(self):
        items = self.run_check(r1=" network 10.1.0.0 0.0.255.255 area 0\n network 10.0.0.0 0.0.0.3 area 0")
        self.assertTrue(any(lvl == "WARN" and "wider than" in msg for lvl, msg in items))

    def test_wrong_gateway_is_an_error(self):
        items = self.run_check(pc="ip 10.1.1.10 10.1.1.254 24")
        self.assertIn(("ERROR", "gateway 10.1.1.254 is not a router address on its segment"), items)

    def test_host_in_wrong_subnet_is_an_error(self):
        items = self.run_check(pc="ip 10.9.9.10 10.1.1.1 24")
        self.assertTrue(any(lvl == "ERROR" and "mixes subnets" in msg for lvl, msg in items))

    def test_wildcard_matching(self):
        n = labcheck.Network(labcheck.ip.IPv4Address("10.0.0.0"), labcheck.ip.IPv4Address("0.0.0.3"), "0")
        self.assertTrue(n.matches(labcheck.ip.IPv4Address("10.0.0.2")))
        self.assertFalse(n.matches(labcheck.ip.IPv4Address("10.0.0.4")))
        self.assertEqual(n.prefixlen, 30)

    def test_interface_abbreviations(self):
        self.assertEqual(labcheck.expand("s6/0"), "Serial6/0")
        self.assertEqual(labcheck.expand("f0/0"), "FastEthernet0/0")
        self.assertEqual(labcheck.expand("e1"), "Ethernet1")


if __name__ == "__main__":
    unittest.main()
