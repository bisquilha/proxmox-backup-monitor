import re
import sys

import yaml

z = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["zabbix_export"]
assert z["version"] == "7.4"
tpl = z["templates"][0]
T = tpl["template"]
uuids = []


def walk(o):
    if isinstance(o, dict):
        if "uuid" in o:
            uuids.append(o["uuid"])
        for v in o.values():
            walk(v)
    elif isinstance(o, list):
        for v in o:
            walk(v)


walk(z)
assert len(uuids) == len(set(uuids)), "uuid duplicado"
assert all(re.fullmatch(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", x) for x in uuids)
items = tpl["items"]
master = items[0]["key"]
assert master == "vfs.file.contents[/var/lib/pve-backup-monitor/tests.json]" and items[0]["delay"] == "1m" and items[0]["history"] == "1d"
keys = {i["key"] for i in items}
assert len(keys) == len(items)
for i in items[1:]:
    assert i["type"] == "DEPENDENT" and i["master_item"]["key"] == master, i["key"]
    steps = [s["type"] for s in i["preprocessing"]]
    assert steps[0] == "JSONPATH"
    if i["key"] == "pve.tests.generated":
        assert "DISCARD_UNCHANGED_HEARTBEAT" not in steps
    else:
        assert steps[-1] == "DISCARD_UNCHANGED_HEARTBEAT" and i["preprocessing"][-1]["parameters"] == ["1h"]
    assert i.get("units", "") not in ("h", "GB", "d", "dias"), "unidade sem '!' vai ser escalada: %s" % i["key"]
assert "triggers" not in tpl
for t in z["triggers"]:
    assert t["priority"] in ("INFO", "WARNING", "AVERAGE", "HIGH", "DISASTER")
    for tpl_name, key in re.findall(r"\(/([^/]+)/([^,)]+(?:\[[^\]]*\])?)", t["expression"]):
        assert tpl_name == T and key in keys, (t["name"], tpl_name, key)
print("template ok:", len(items), "itens,", len(z["triggers"]), "triggers,", len(uuids), "uuids")
