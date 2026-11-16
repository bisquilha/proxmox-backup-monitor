import datetime
import importlib.util
import json
import re
import sys

import yaml

doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
z = doc["zabbix_export"]
assert z["version"] == "7.4"
tpl = z["templates"][0]
T = tpl["template"]

# uuids: unicos e v4
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
for x in uuids:
    assert re.fullmatch(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", x), x
print("uuids ok:", len(uuids))

host_keys = {i["key"] for i in tpl["items"]}
master = tpl["items"][0]["key"]
assert tpl["items"][0]["history"] == "1d"
assert all(i.get("master_item", {}).get("key") == master for i in tpl["items"][1:])
dr = tpl["discovery_rules"][0]
assert dr["master_item"]["key"] == master and dr["type"] == "DEPENDENT"
protos = dr["item_prototypes"]
proto_keys = {p["key"] for p in protos}
assert all(p["master_item"]["key"] == master for p in protos)
assert len(proto_keys) == len(protos) == 9, proto_keys
assert "triggers" not in tpl
assert tpl["items"][0]["delay"] == "1m", tpl["items"][0]["delay"]
dep = [i for i in tpl["items"][1:]] + list(protos)
for i in dep:
    steps = [x["type"] for x in i["preprocessing"]]
    if i["key"] == "pve.backup.generated":
        assert "DISCARD_UNCHANGED_HEARTBEAT" not in steps
    else:
        assert steps[-1] == "DISCARD_UNCHANGED_HEARTBEAT" and i["preprocessing"][-1]["parameters"] == ["1h"], i["key"]
print("heartbeat ok em", sum(1 for i in dep if i["key"] != "pve.backup.generated"), "itens")
for p in protos:
    if p.get("units") in ("h", "GB"):
        raise AssertionError("unidade sem '!' vai ser escalada: %s" % p["key"])
print("itens:", len(host_keys), "| prototipos:", len(protos))

exprs = [t["expression"] for t in z["triggers"]] + [t["expression"] for t in dr["trigger_prototypes"]]
allkeys = host_keys | proto_keys
for e in exprs:
    refs = [re.sub(r",\d+[smhd]$", "", r) for r in re.findall(r"\(/%s/(.+?)\)" % re.escape(T), e)]
    assert refs, e
    for r in refs:
        assert r in allkeys, (r, e)
print("triggers referenciam itens existentes:", len(exprs))

# campos lidos pelo template existem e nao sao nulos no JSON real do coletor
spec = importlib.util.spec_from_file_location("m", sys.argv[2])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime.datetime(2026, 10, 7, 14, 0).timestamp()
guests = [
    {"vmid": "3001", "name": "srv19", "type": "qemu", "status": "running", "pool": None, "tags": [], "template": False},
    {"vmid": "101", "name": "PD1", "type": "qemu", "status": "running", "pool": None, "tags": [], "template": False},
    {"vmid": "3009", "name": "LAB3", "type": "qemu", "status": "stopped", "pool": None, "tags": ["sem-backup"], "template": False},
]
jobs = [{"id": "w", "enabled": 1, "schedule": "sun 01:00", "storage": "s", "vmid": "3001,101"}]
backups = {"101": [{"ctime": NOW - 3.5 * 86400, "size": 153.9e9}]}
runs = {"3001": {"result": "failed", "ts": int(NOW - 3600), "msg": 'VM is locked "backup"\\ x'}}
rows = m.evaluate(guests, jobs, backups, NOW, {"3001": 1670964959}, "pve3", runs)
rep = json.loads(json.dumps(m.make_report("pve3", rows, jobs, [], NOW)))

fields = set()
for p in protos:
    mt = re.fullmatch(r"\$\.guests\[\?\(@\.vmid=='\{#VMID\}'\)\]\.(\w+)\.first\(\)", p["preprocessing"][0]["parameters"][0])
    assert mt, p["key"]
    fields.add(mt.group(1))
for f in sorted(fields):
    for g in rep["guests"]:
        assert f in g and g[f] is not None, (f, g["vmid"])
print("campos por VM lidos pelo template:", sorted(fields))
for path in ("generated",):
    assert isinstance(rep[path], int)
for k in ("problems", "job_errors_14d"):
    assert isinstance(rep["summary"][k], int)
for mac_path in dr["lld_macro_paths"]:
    f = mac_path["path"].lstrip("$.")
    assert all(f in g for g in rep["guests"]), f
by = {g["vmid"]: g for g in rep["guests"]}
assert by["3001"]["state"] == "falhou" and by["3001"]["last_run_msg"] == 'VM is locked "backup"\\ x'
assert "null" not in json.dumps(rep["guests"])
print("TEMPLATE VALIDO (estrutura local; a importacao real e no Zabbix)")
