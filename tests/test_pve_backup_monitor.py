import datetime
import importlib.util
import json
import os
import sys
import tempfile

SCRIPT = sys.argv[1]
spec = importlib.util.spec_from_file_location("m", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def ep(*a):
    return datetime.datetime(*a).timestamp()


def G(vmid, name=None, typ="qemu", status="running", pool=None, tags=None, template=False):
    return {"vmid": str(vmid), "name": name or "vm%s" % vmid, "type": typ, "status": status,
            "pool": pool, "tags": tags or [], "template": template}


def states(rows):
    return {r["vmid"]: r["state"] for r in rows}


NOW = ep(2026, 10, 7, 14, 0)
WEEKLY = [{"id": "w", "enabled": 1, "schedule": "sat 01:00", "storage": "s", "vmid": "1,2,3,4,5,6,7,8"}]

# --- schedule parser e limites
assert m.parse_period_hours("sat 01:00") == 168
assert m.parse_period_hours("21:00") == 24
assert m.parse_period_hours("*-*-01 02:00") == 744
assert m.parse_period_hours("mon..fri 21:00") == 72
assert m.parse_period_hours("mon,wed 03:00") == 120
assert m.parse_period_hours("blabla") is None
assert abs(m.threshold_hours(24) - 30) < 1e-6 and abs(m.threshold_hours(168) - 193.2) < 1e-6
assert m.parse_tags("a;Sem-Backup, b") == ["a", "sem-backup", "b"] and m.parse_tags(None) == []

# --- parser do log do vzdump
OK_LOG = ("INFO: starting new backup job: vzdump 3001 --storage nas-backup\n"
          "INFO: Starting Backup of VM 3001 (qemu)\nINFO: Backup started at 2026-10-04 01:50:36\n"
          "INFO: 12% (6.0 GiB of 50.0 GiB) in 3s\nINFO: Finished Backup of VM 3001 (00:02:04)\n"
          "INFO: Backup finished at 2026-10-04 01:52:40\nINFO: Backup job finished successfully\n")
BAD_LOG = ("INFO: Starting Backup of VM 3001 (qemu)\nINFO: Backup started at 2026-02-27 21:00:01\n"
           "ERROR: Backup of VM 3001 failed - VM is locked (backup)\nINFO: Failed at 2026-02-27 21:00:03\n"
           "INFO: Backup job finished with errors\n")
CT_LOG = "INFO: Starting Backup of VM 3006 (lxc)\nINFO: Finished Backup of VM 3006 (00:00:30)\n"
HALF_LOG = "INFO: Starting Backup of VM 9 (qemu)\nINFO: 40% (20 GiB of 50 GiB) in 5m\n"
r = m.parse_vzdump_log(OK_LOG, NOW - 3600, NOW)
assert r["result"] == "ok" and r["ts"] == int(NOW - 3600)
r = m.parse_vzdump_log(BAD_LOG, NOW - 3600, NOW)
assert r["result"] == "failed" and r["msg"] == "VM is locked (backup)"
assert m.parse_vzdump_log(CT_LOG, NOW, NOW)["result"] == "ok"
assert m.parse_vzdump_log(HALF_LOG, NOW - 600, NOW)["result"] == "running"
rr = m.parse_vzdump_log(HALF_LOG, NOW - 7 * 3600, NOW)
assert rr["result"] == "failed" and "interrompido" in rr["msg"]

# --- pve3 com dados reais do inventario de 2026-10-07
ids3 = [101, 200, 1000, 3001, 3002, 3005, 3006, 3007, 3009, 3013, 3014, 3015, 3016, 3017]
guests3 = [G(v, typ="lxc" if v == 3006 else "qemu", tags=["sem-backup"] if v == 3009 else None) for v in ids3]
jobs3 = [
    {"id": "backup-74a0b07b-885e", "enabled": 1, "node": "pve3", "schedule": "sun 01:00", "storage": "nas-backup",
     "vmid": "3006,3002,3005,3007,3014,3013,101,3016,200,1000,3001"},
    {"id": "backup-6f51d59a-ef82", "enabled": 1, "node": "pve3", "schedule": "21:00", "storage": "nas-backup", "vmid": "3001"},
    {"id": "b355ab67-6714", "enabled": 1, "schedule": "*-*-01 02:00", "storage": "nas-backup", "vmid": "3015,3017"},
]
sun = ep(2026, 10, 4, 1, 0)
backups3 = {str(v): [{"ctime": sun, "size": 5e9}] for v in (101, 200, 1000, 3002, 3005, 3006, 3007, 3013, 3014, 3016)}
backups3["3015"] = [{"ctime": ep(2026, 10, 1, 2, 0), "size": 241e9}]
ctimes3 = {"3001": 1670964959, "3017": 1791221477}
runs3 = {"3001": {"result": "failed", "ts": int(ep(2026, 2, 27, 21, 0)), "msg": "VM is locked (backup)"}}
rows3 = m.evaluate(guests3, jobs3, backups3, NOW, ctimes3, "pve3", runs3)
by = {r["vmid"]: r for r in rows3}
# falha de fevereiro e antiga demais (>> limite do job): nao vira 'falhou', e sem backup nenhum
assert by["3001"]["state"] == "sem_backup" and by["3001"]["last_run_result"] == "failed"
assert by["3009"]["state"] == "isento" and by["3015"]["state"] == "ok" and by["101"]["state"] == "ok"
assert by["3017"]["state"] == "aguardando"          # srv35 agora esta no job mensal

# --- falhou: falha recente e posterior ao ultimo backup
rec = {"result": "failed", "ts": int(NOW - 3600), "msg": "boom"}
old_ok = [{"ctime": NOW - 3 * 86400, "size": 1e9}]
rows = m.evaluate([G(1), G(2), G(3), G(4)], WEEKLY, {"1": old_ok, "3": [{"ctime": NOW - 600, "size": 1e9}], "4": old_ok},
                  NOW, {"2": NOW - 86400 * 20}, "n", {"1": rec, "2": rec, "3": rec,
                                                      "4": {"result": "failed", "ts": int(NOW - 4 * 86400 - 3600), "msg": "x"}})
st = states(rows)
assert st["1"] == "falhou"          # tinha backup antigo mas a ultima execucao falhou
assert st["2"] == "falhou"          # nunca teve backup e a ultima execucao falhou
assert st["3"] == "ok"              # falha mais velha que o ultimo backup bem-sucedido
assert st["4"] == "ok"              # falha de 4 dias atras, anterior ao backup de 3 dias atras
msg = [r for r in rows if r["vmid"] == "1"][0]["last_run_msg"]
assert msg == "boom"
# falha sem job nao conta como 'falhou'
assert m.evaluate([G(9)], [], {}, NOW, {}, "n", {"9": rec})[0]["state"] == "sem_job"

# --- tolerancia de 48h para VM nova sem job; aguardando/sem_backup em job
r = m.evaluate([G(1), G(2), G(3)], [], {}, NOW, {"1": NOW - 3600 * 47, "2": NOW - 3600 * 49}, "n")
assert states(r) == {"1": "novo_sem_job", "2": "sem_job", "3": "sem_job"}
jobs_w = [{"id": "w", "enabled": 1, "schedule": "sat 01:00", "storage": "s", "vmid": "7,8"}]
r = m.evaluate([G(7), G(8)], jobs_w, {}, NOW, {"7": NOW - 3600 * 50, "8": NOW - 3600 * 24 * 9}, "n")
assert states(r) == {"7": "aguardando", "8": "sem_backup"}

# --- stale, pool, all/exclude, desabilitado/outro no, template, tag
r = m.evaluate([G(1000)], [{"id": "w", "enabled": 1, "schedule": "sat 01:00", "storage": "s", "vmid": "1000"}],
               {"1000": [{"ctime": ep(2026, 9, 12, 1, 0), "size": 5e9}]}, NOW, {}, "pve1")
assert r[0]["state"] == "stale"
jobs_p = [{"id": "p", "enabled": 1, "schedule": "21:00", "storage": "s", "pool": "prod"}]
r = m.evaluate([G(1, pool="prod"), G(2, pool="dev"), G(3)], jobs_p, {"1": [{"ctime": NOW - 3600, "size": 1e9}]}, NOW, {}, "n")
assert states(r) == {"1": "ok", "2": "sem_job", "3": "sem_job"}
jobs_a = [{"id": "a", "enabled": 1, "schedule": "21:00", "storage": "s", "all": 1, "exclude": "6"}]
r = m.evaluate([G(5), G(6)], jobs_a, {"5": [{"ctime": NOW - 3600, "size": 1e9}]}, NOW, {}, "n")
assert states(r) == {"5": "ok", "6": "sem_job"}
jobs_x = [{"id": "a", "enabled": 0, "schedule": "21:00", "storage": "s", "vmid": "5"},
          {"id": "b", "enabled": 1, "node": "outro", "schedule": "21:00", "storage": "s", "vmid": "5"}]
assert m.evaluate([G(5)], jobs_x, {}, NOW, {}, "n")[0]["state"] == "sem_job"
r = m.evaluate([G(1, template=True), G(2, tags=["sem-backup"])], [], {}, NOW, {}, "n")
assert [x["state"] for x in r] == ["isento", "isento"]
assert m.evaluate([G(1000)], [], {}, NOW, {}, "pve1")[0]["key"] != m.evaluate([G(1000)], [], {}, NOW, {}, "pve3")[0]["key"]

# --- tasks
tasks = [
    {"starttime": NOW - 86400, "endtime": NOW - 80000, "status": "OK"},
    {"starttime": NOW - 2 * 86400, "endtime": NOW - 2 * 86400 + 60, "status": "job errors"},
    {"starttime": NOW - 3 * 86400, "endtime": NOW - 3 * 86400 + 60, "status": "WARNINGS: 1"},
    {"starttime": NOW - 40 * 86400, "endtime": NOW - 40 * 86400 + 60, "status": "job errors"},
    {"starttime": NOW - 100, "status": None},
]
ts = m.summarize_tasks(tasks, NOW)
assert ts["job_errors_14d"] == 1 and ts["job_warnings_14d"] == 1 and ts["last_task_status"] == "OK", ts

# --- relatorio: sem nulos, campos que o template usa
rep = m.make_report("pve3", rows3, jobs3, tasks, NOW)
txt = json.dumps(rep)
assert "null" not in json.dumps(rep["guests"]), "JSON dos guests nao pode ter nulos"
g3001 = [g for g in rep["guests"] if g["vmid"] == "3001"][0]
assert g3001["age_h"] == -1 and g3001["last_backup"] == 0 and g3001["last_size_gb"] == 0
assert g3001["last_run_result"] == "failed" and g3001["last_run_msg"] == "VM is locked (backup)"
for g in rep["guests"]:
    for k in ("vmid", "name", "node", "state", "severity", "age_h", "last_backup", "last_size_gb", "copies",
              "last_run_result", "last_run_ts", "last_run_msg"):
        assert k in g and g[k] is not None, (g["vmid"], k)
assert rep["version"] == 3 and rep["summary"]["jobs"] == 3 and rep["summary"]["problems"] == 1
assert "falhou" in rep["summary"]

# --- cron
tmpd = tempfile.mkdtemp()
cron = os.path.join(tmpd, "pve-backup-monitor")
m.install_cron(cron, "/usr/local/bin/pve-backup-monitor.py")
t = open(cron).read()
assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in t and "* * * * * root flock -n /run/pve-backup-monitor.lock /usr/bin/python3 /usr/local/bin/pve-backup-monitor.py --write --quiet" in t

m.print_table("pve3", rows3, rep["summary"])
print("\nTODOS OS TESTES PASSARAM")
