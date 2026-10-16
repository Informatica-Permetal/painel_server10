from painel import parsers
from painel.config import parse_baseline

from .conftest import fixture


def test_megacli_ldinfo():
    vds = parsers.parse_megacli_ldinfo(fixture("megacli_ldinfo.txt"))
    assert vds == [{"id": 0, "name": "RAID1-TESTE", "raid_level": "RAID 1", "size": "278.875 GB",
                    "state": "Optimal", "drives": 2}]


def test_megacli_pdlist():
    pds = parsers.parse_megacli_pdlist(fixture("megacli_pdlist.txt"))
    assert [p["device_id"] for p in pds] == [0, 1]
    first = pds[0]
    assert first["slot"] == 0
    assert first["state"] == "Online, Spun Up"
    assert first["model"] == "ST3300657SS"
    assert first["media_errors"] == 3
    assert first["predictive_failures"] == 0
    assert first["temperature"] == 34
    assert first["smart_alert"] is False


def test_megacli_bbu():
    bbu = parsers.parse_megacli_bbu(fixture("megacli_bbu.txt"))
    assert bbu["battery state"] == "Optimal"
    assert bbu["voltage"] == "4012 mV"
    assert bbu["voltage status"] == "OK"
    assert bbu["battery replacement required"] == "No"


def test_smartctl_sas():
    data = parsers.parse_smartctl(fixture("smartctl_0.txt"))
    assert data["health"] == "OK"
    assert data["serial"] == "TESTSER0"
    assert data["grown_defects"] == 9
    assert data["read_uncorrected"] == 1
    assert data["write_uncorrected"] == 0
    assert data["verify_uncorrected"] == 0
    assert data["non_medium"] == 7
    assert data["temperature"] == 34
    assert data["power_on_hours"] == 70000


def test_smartctl_sem_dados_retorna_none():
    assert parsers.parse_smartctl("Smartctl open device: /dev/sda failed: No such device") is None


def test_ipmi_elist_e_chassis():
    sensors = parsers.parse_ipmi_sdr_elist(fixture("ipmi_sdr_elist.txt"))
    fan5 = next(s for s in sensors if s["name"] == "FAN MOD 5A RPM")
    assert fan5["status"] == "ns" and fan5["reading"] == "Disabled"
    chassis = parsers.parse_kv(fixture("ipmi_chassis.txt"))
    assert chassis["Drive Fault"] == "false"
    sel = parsers.parse_ipmi_sel_info(fixture("ipmi_sel_info.txt"))
    assert sel == {"entries": 1, "percent_used": 0}


def test_proc():
    first = parsers.parse_cpu_stat("cpu  1000 0 500 8000 100 0 10 0 0 0\ncpu0 1 1 1 1\ncpu1 1 1 1 1\n")
    second = parsers.parse_cpu_stat("cpu  1100 0 550 8800 100 0 10 0 0 0\ncpu0 1 1 1 1\ncpu1 1 1 1 1\n")
    assert first[2] == 2
    assert round(parsers.cpu_percent(first, second)) == 16
    mounts = parsers.parse_mounts(
        "proc /proc proc rw 0 0\n/dev/sda2 / ext4 rw 0 0\n/dev/sda2 /var/x ext4 rw 0 0\n"
        "/dev/loop0 /snap/a squashfs ro 0 0\n/dev/sda1 /boot/efi vfat rw 0 0\n/dev/sdb1 /mnt/a\\040b xfs rw 0 0\n")
    assert mounts == ["/", "/boot/efi", "/mnt/a b"]


def test_baseline_config():
    seeds = parse_baseline("0:grown_defects=12,read_uncorrected=3;1:non_medium=40")
    assert seeds == {"smart.0.grown_defects": 12, "smart.0.read_uncorrected": 3, "smart.1.non_medium": 40}
