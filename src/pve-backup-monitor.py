#!/usr/bin/env python3
"""pve-backup-monitor.py (v3) - estado de backup de cada guest deste no Proxmox.

READ-ONLY (so consulta pvesh/qm e le /var/log/vzdump). Por guest, compara o backup
mais recente nos storages dos jobs com o intervalo esperado pelo proprio agendamento
do job e le o resultado da ultima execucao do vzdump. Nada fixo no codigo: guests,
jobs (vmid, all, exclude, pool) e backups sao descobertos a cada execucao. Escreve um
JSON que o Zabbix le (vfs.file.contents).

Isencao: tag 'sem-backup' na VM (qm set <id> --tags sem-backup). Templates sao isentos.

Estados (severity):
  ok(0) isento(0) novo_sem_job(1) aguardando(1)
  falhou(2) stale(2) sem_backup(2) sem_job(2)

No JSON nao ha valores nulos: idade/tamanho/periodo ausentes viram -1 ou 0.

Uso:
  pve-backup-monitor.py            # tabela na tela
  pve-backup-monitor.py --write    # tabela + grava o JSON (cron)
  pve-backup-monitor.py --json     # imprime o JSON
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin:" + os.environ.get("PATH", "")

VERSION = 3
DEFAULT_OUT = "/var/lib/pve-backup-monitor/status.json"
DEFAULT_CRON = "/etc/cron.d/pve-backup-monitor"
VZDUMP_LOG_DIR = "/var/log/vzdump"
EXEMPT_TAG = "sem-backup"
NEW_VM_GRACE_H = 48
INCOMPLETE_LOG_H = 6
TASK_WINDOW_DAYS = 14

DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_period_hours(sched):
    """Intervalo (horas) entre execucoes de um schedule do PVE, ou None se nao entendi."""
    s = (sched or "").strip().lower()
    if not s:
        return None
    parts = s.split()
    if len(parts) == 1:
        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", parts[0]):
            return 24.0
        return None
    head = parts[0]
    if re.match(r"^(\*|\d{4})-\*-\d{1,2}$", head):
        return 31 * 24.0
    days = set()
    for tok in head.split(","):
        if ".." in tok:
            a, b = tok.split("..", 1)
            a, b = DOW.get(a[:3]), DOW.get(b[:3])
            if a is None or b is None:
                return None
            i = a
            while True:
                days.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        else:
            d = DOW.get(tok[:3])
            if d is None:
                return None
            days.add(d)
    if not days:
        return None
    ordered = sorted(days)
    if len(ordered) == 1:
        return 7 * 24.0
    gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    gaps.append(7 - ordered[-1] + ordered[0])
    return max(gaps) * 24.0


def threshold_hours(period_h):
    grace = min(max(6.0, period_h * 0.15), 96.0)
    return period_h + grace


def csv_set(value):
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def covering_jobs(g, node, jobs):
    vmid = str(g["vmid"])
    pool = g.get("pool") or ""
    out = []
    for j in jobs:
        if not j.get("enabled", 1):
            continue
        if j.get("node") and j["node"] != node:
            continue
        excluded = vmid in csv_set(j.get("exclude"))
        hit = (
            (j.get("all") and not excluded)
            or vmid in csv_set(j.get("vmid"))
            or (j.get("pool") and pool == j["pool"] and not excluded)
        )
        if hit:
            out.append(j)
    return out


def parse_vzdump_log(text, mtime, now):
    """Resultado da ultima execucao do vzdump de um guest, a partir do log dele."""
    ok = [m.end() for m in re.finditer(r"Finished Backup of (?:VM|CT) \d+", text)]
    bad = list(re.finditer(r"^ERROR: Backup of (?:VM|CT) \d+ failed - (.*)$", text, re.M))
    last_ok = ok[-1] if ok else -1
    last_bad = bad[-1].start() if bad else -1
    if last_bad > last_ok:
        return {"result": "failed", "ts": int(mtime), "msg": bad[-1].group(1).strip()[:200]}
    if last_ok >= 0:
        return {"result": "ok", "ts": int(mtime), "msg": ""}
    if now - mtime > INCOMPLETE_LOG_H * 3600:
        return {"result": "failed", "ts": int(mtime), "msg": "interrompido: log sem conclusao"}
    return {"result": "running", "ts": int(mtime), "msg": ""}


NO_RUN = {"result": "none", "ts": 0, "msg": ""}


def evaluate(guests, jobs, backups, now, ctimes, node, runs=None):
    """guests: [{vmid,name,type,status,pool,tags,template}], backups: {vmid: [{ctime,size}]},
    ctimes: {vmid: epoch de criacao}, runs: {vmid: resultado do ultimo vzdump}."""
    runs = runs or {}
    rows = []
    for g in sorted(guests, key=lambda x: int(x["vmid"])):
        vid = str(g["vmid"])
        cj = covering_jobs(g, node, jobs)
        periods = []
        for j in cj:
            p = parse_period_hours(j.get("schedule"))
            periods.append(p if p else 7 * 24.0)
        period_h = min(periods) if periods else None
        thr_h = threshold_hours(period_h) if period_h else None
        bl = sorted(backups.get(vid, []), key=lambda b: b["ctime"])
        last = bl[-1] if bl else None
        age_h = (now - last["ctime"]) / 3600.0 if last else None
        ct = ctimes.get(vid)
        age_vm_h = (now - ct) / 3600.0 if ct else None
        run = runs.get(vid) or NO_RUN
        run_age_h = (now - run["ts"]) / 3600.0 if run["ts"] else None

        failed = bool(
            cj and run["result"] == "failed" and run_age_h is not None and run_age_h < thr_h
            and (last is None or run["ts"] > last["ctime"])
        )

        reason = None
        if g.get("template"):
            reason = "template"
        elif EXEMPT_TAG in (g.get("tags") or []):
            reason = "tag"

        if reason:
            state, sev = "isento", 0
        elif not cj:
            if age_vm_h is not None and age_vm_h < NEW_VM_GRACE_H:
                state, sev = "novo_sem_job", 1
            else:
                state, sev = "sem_job", 2
        elif failed:
            state, sev = "falhou", 2
        elif last is None:
            if age_vm_h is not None and age_vm_h < thr_h:
                state, sev = "aguardando", 1
            else:
                state, sev = "sem_backup", 2
        elif age_h > thr_h:
            state, sev = "stale", 2
        else:
            state, sev = "ok", 0

        rows.append({
            "key": "%s/%s" % (node, vid),
            "node": node,
            "vmid": vid,
            "name": g.get("name", "?"),
            "type": g.get("type", "qemu"),
            "status": g.get("status", "?"),
            "tags": g.get("tags") or [],
            "exempt_reason": reason,
            "jobs": [str(j.get("id", "?")) for j in cj],
            "period_h": period_h,
            "threshold_h": round(thr_h, 1) if thr_h else None,
            "last_backup": last["ctime"] if last else None,
            "age_h": round(age_h, 1) if age_h is not None else None,
            "last_size_gb": round(last["size"] / 1e9, 1) if last else None,
            "copies": len(bl),
            "last_run_result": run["result"],
            "last_run_ts": run["ts"],
            "last_run_msg": run["msg"],
            "state": state,
            "severity": sev,
        })
    return rows


SENTINEL = {"period_h": 0, "threshold_h": 0, "last_backup": 0, "age_h": -1, "last_size_gb": 0}


def jsonable(rows):
    """Sem nulos no JSON (o Zabbix trata nulo como valor invalido)."""
    out = []
    for r in rows:
        r = dict(r)
        for k, v in SENTINEL.items():
            if r.get(k) is None:
                r[k] = v
        if r.get("exempt_reason") is None:
            r["exempt_reason"] = ""
        out.append(r)
    return out


def summarize_tasks(tasks, now):
    cutoff = now - TASK_WINDOW_DAYS * 86400
    errors = warnings = 0
    last = None
    for t in sorted(tasks, key=lambda x: x.get("starttime", 0)):
        if not t.get("endtime"):
            continue
        last = t
        if t.get("starttime", 0) < cutoff:
            continue
        st = str(t.get("status", ""))
        if st == "OK":
            continue
        if st.startswith("WARNINGS"):
            warnings += 1
        else:
            errors += 1
    return {
        "job_errors_14d": errors,
        "job_warnings_14d": warnings,
        "last_task_status": last.get("status") if last else None,
        "last_task_end": last.get("endtime") if last else None,
    }


def job_info(j):
    if j.get("all"):
        scope = "all"
    elif j.get("pool"):
        scope = "pool:%s" % j["pool"]
    else:
        scope = "vmid:%s" % j.get("vmid", "")
    return {
        "id": str(j.get("id", "?")),
        "enabled": bool(j.get("enabled", 1)),
        "schedule": j.get("schedule"),
        "storage": j.get("storage"),
        "scope": scope,
        "mailto": bool(j.get("mailto")),
    }


# ---------------------------------------------------------------- coleta
def run_json(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError("%s -> %s" % (" ".join(cmd), r.stderr.strip()[:200]))
    return json.loads(r.stdout)


def guest_ctime(vid, typ):
    if typ != "qemu":
        return None
    r = subprocess.run(["qm", "config", vid], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=30)
    m = re.search(r"^meta:.*ctime=(\d+)", r.stdout, re.M)
    return int(m.group(1)) if m else None


def read_run(vid, typ, now):
    path = os.path.join(VZDUMP_LOG_DIR, "%s-%s.log" % ("lxc" if typ == "lxc" else "qemu", vid))
    try:
        mtime = os.stat(path).st_mtime
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        return dict(NO_RUN)
    return parse_vzdump_log(text, mtime, now)


def parse_tags(raw):
    return [t.strip().lower() for t in re.split(r"[;,]", raw or "") if t.strip()]


def collect(node, now):
    res = [r for r in run_json(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"])
           if r.get("node") == node]
    guests = [{"vmid": str(r["vmid"]), "name": r.get("name", "?"), "type": r.get("type", "qemu"),
               "status": r.get("status", "?"), "pool": r.get("pool"), "tags": parse_tags(r.get("tags")),
               "template": bool(r.get("template"))} for r in res]
    jobs = [j for j in run_json(["pvesh", "get", "/cluster/backup", "--output-format", "json"])
            if not j.get("node") or j["node"] == node]

    storages = set(j["storage"] for j in jobs if j.get("enabled", 1) and j.get("storage"))
    backups = {}
    for s in sorted(storages):
        items = run_json(["pvesh", "get", "/nodes/%s/storage/%s/content" % (node, s),
                          "--content", "backup", "--output-format", "json"])
        for b in items:
            backups.setdefault(str(b.get("vmid")), []).append({"ctime": b["ctime"], "size": b.get("size", 0)})

    ctimes = {}
    runs = {}
    for g in guests:
        runs[g["vmid"]] = read_run(g["vmid"], g["type"], now)
        if g["vmid"] not in backups:
            ct = guest_ctime(g["vmid"], g["type"])
            if ct:
                ctimes[g["vmid"]] = ct
    try:
        tasks = run_json(["pvesh", "get", "/nodes/%s/tasks" % node, "--typefilter", "vzdump",
                          "--limit", "200", "--output-format", "json"])
    except Exception:
        tasks = []
    return guests, jobs, backups, ctimes, runs, tasks


def fmt_age(h):
    if h is None:
        return "-"
    return "%.1fh" % h if h < 48 else "%.1fd" % (h / 24.0)


def print_table(node, rows, summary):
    print("no: %s" % node)
    print("%-6s %-20s %-12s %-8s %9s %8s %4s %-8s  %s" % (
        "VMID", "NOME", "ESTADO", "STATUS", "IDADE", "LIMITE", "COP", "ULT.JOB", "JOB"))
    for r in sorted(rows, key=lambda x: (-x["severity"], int(x["vmid"]))):
        print("%-6s %-20s %-12s %-8s %9s %8s %4s %-8s  %s" % (
            r["vmid"], str(r["name"])[:20], r["state"].upper(), r["status"], fmt_age(r["age_h"]),
            fmt_age(r["threshold_h"]), r["copies"], r["last_run_result"],
            ",".join(j[:14] for j in r["jobs"]) or "-"))
        if r["state"] == "falhou" and r["last_run_msg"]:
            print("       -> %s" % r["last_run_msg"])
    s = summary
    print("\nresumo: %d guests | ok=%d isento=%d aguardando=%d novo_sem_job=%d | PROBLEMAS=%d | "
          "erros de job (14d)=%d, avisos=%d | ultima task: %s" % (
              s["guests"], s["ok"], s["isento"], s["aguardando"], s["novo_sem_job"], s["problems"],
              s["job_errors_14d"], s["job_warnings_14d"], s["last_task_status"]))


def make_report(node, rows, jobs, tasks, now):
    summary = {"guests": len(rows), "problems": sum(1 for r in rows if r["severity"] >= 2)}
    for st in ("ok", "isento", "novo_sem_job", "aguardando", "falhou", "stale", "sem_backup", "sem_job"):
        summary[st] = sum(1 for r in rows if r["state"] == st)
    summary["worst"] = max([r["severity"] for r in rows] or [0])
    summary["jobs"] = len(jobs)
    summary.update(summarize_tasks(tasks, now))
    return {"version": VERSION, "generated": int(now), "node": node, "summary": summary,
            "guests": jsonable(rows), "jobs": [job_info(j) for j in jobs]}


def install_cron(cron_path, script_path):
    """Agenda a coleta a cada 1 min; flock evita duas execucoes ao mesmo tempo (idempotente)."""
    line = ("# gerado por pve-backup-monitor.py --install\n"
            "PATH=/usr/sbin:/usr/bin:/sbin:/bin\n"
            "* * * * * root flock -n /run/pve-backup-monitor.lock /usr/bin/python3 %s --write --quiet\n" % script_path)
    with open(cron_path, "w") as f:
        f.write(line)
    os.chmod(cron_path, 0o644)
    return line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="grava o JSON em --out")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--json", action="store_true", help="imprime o JSON em vez da tabela")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--install", action="store_true", help="agenda no cron (a cada 1 min) e grava o JSON agora")
    ap.add_argument("--cron-file", default=DEFAULT_CRON)
    a = ap.parse_args()

    if a.install:
        script = os.path.abspath(__file__)
        os.chmod(script, 0o755)
        install_cron(a.cron_file, script)
        print("agendado em %s (a cada 1 min)" % a.cron_file)
        a.write = True

    node = socket.gethostname().split(".")[0]
    now = time.time()
    guests, jobs, backups, ctimes, runs, tasks = collect(node, now)
    rows = evaluate(guests, jobs, backups, now, ctimes, node, runs)
    report = make_report(node, rows, jobs, tasks, now)

    if a.json:
        print(json.dumps(report, indent=1))
    elif not a.quiet:
        print_table(node, rows, report["summary"])
    if a.write:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        tmp = a.out + ".tmp"
        with open(tmp, "w") as f:
            json.dump(report, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, a.out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("ERRO: %s" % e, file=sys.stderr)
        sys.exit(1)
