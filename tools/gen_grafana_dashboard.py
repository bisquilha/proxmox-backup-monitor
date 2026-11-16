"""Painel de detalhes "Backups Proxmox (v2)": mesmos dados do painel original (status.json / tests.json do Zabbix), layout novo.

Uso: python gen_grafana_dashboard_v2.py saida.json     (grava tambem saida.json.sql.json com as consultas, para os testes)

Mudancas em relacao ao painel original (gen_grafana_dashboard.py):
  - faixa unica de situacao + lista curta de problemas (backup E teste de restore) no lugar do painel enorme que ficava vazio;
  - 4 blocos de resumo com cor por significado (nao seis iguais);
  - tabela de VMs com 10 colunas que cabem na tela (sem rolagem horizontal), nomes que dizem o que sao e coluna de teste de restore;
  - jobs com a lista de VMs cobertas (nao o ID do Proxmox) e agenda em portugues;
  - dados tecnicos (VMID, mensagem do vzdump...) numa secao recolhida.
"""
import json
import sys

OUT = sys.argv[1]
DS = {"type": "grafana-postgresql-datasource", "uid": "zbx-db-ro"}
MASTER = "vfs.file.contents[/var/lib/pve-backup-monitor/status.json]"
TESTS_KEY = "vfs.file.contents[/var/lib/pve-backup-monitor/tests.json]"
TEST_TAG = "testar"   # igual a require_tag do testador

OK, WARN, BAD, MUTED, INFO = "#2E9E6B", "#D9A441", "#D64545", "#5C6773", "#3F7FBF"

BASE = """with latest as (
  select i.itemid, lt.value::jsonb as doc, lt.clock
  from items i
  cross join lateral (
    select h2.value, h2.clock from history_text h2
    where h2.itemid = i.itemid and h2.clock > extract(epoch from now()) - 86400
    order by h2.clock desc limit 1
  ) lt
  where i.key_ = '%s'
), vms as (
  select h.host as host, gg.g as g
  from latest l
  join items i on i.itemid = l.itemid
  join hosts h on h.hostid = i.hostid
  cross join lateral jsonb_array_elements(l.doc->'guests') as gg(g)
)
""" % MASTER

TESTS_CTE = """, tests as (
  select lt.value::jsonb as doc
  from items i
  cross join lateral (
    select h2.value from history_text h2
    where h2.itemid = i.itemid and h2.clock > extract(epoch from now()) - 172800
    order by h2.clock desc limit 1
  ) lt
  where i.key_ = '%s'
), test_rows as (
  select tt.t->>'host' as host, tt.t->>'vmid' as vmid, tt.t as t
  from tests cross join lateral jsonb_array_elements(tests.doc->'tests') as tt(t)
), tcls as (
  select v.host as host, v.g as g, tr.t as t,
  case
    when not ((v.g->'tags') @> '"%s"'::jsonb) then 998
    when v.g->>'state' = 'sem_job' then 998
    when coalesce(tr.t->>'skip', '') in ('backup-antigo', 'sem-estado') then 998
    when tr.t is null and v.g->>'state' in ('sem_backup', 'sem_job', 'novo_sem_job') then 998
    when tr.t->>'last_result' = 'failed' then -1
    when tr.t is null or (tr.t->>'last_ok_ts') is null then 999
    else least(floor((extract(epoch from now()) - (tr.t->>'last_ok_ts')::numeric) / 86400)::int, 899)
  end as code,
  case
    when coalesce(tr.t->>'restore_ok', 'false') = 'true'
         and (coalesce(tr.t->>'integrity_ok', 'false') <> 'true'
              or coalesce((tr.t->>'restore_ts')::numeric, 0) >= coalesce((tr.t->>'integrity_ts')::numeric, 0)) then 'R'
    when coalesce(tr.t->>'integrity_ok', 'false') = 'true' then 'I'
  end as lvl
  from vms v
  left join test_rows tr on tr.host = v.host and tr.vmid = v.g->>'vmid'
  where v.g->>'state' <> 'isento'
)
""" % (TESTS_KEY, TEST_TAG)

BRT = "(to_timestamp(%s) at time zone 'UTC') - interval '3 hours'"   # horario de Brasilia, sem depender de tabela de fusos
SHORT = "regexp_replace(regexp_replace(%s, '^SITE-HQ-', ''), '^SITE-', '')"
# horas -> texto legivel: ate 48 h em horas, acima em dias
HUMAN_H = """case when coalesce((%(c)s)::numeric, -1) < 0 then null
       when (%(c)s)::numeric < 48 then round((%(c)s)::numeric)::int || ' h'
       else round((%(c)s)::numeric / 24, %(d)s) || ' dias' end"""

SQL = {}
SQL["situacao"] = BASE + TESTS_CTE + """select (
  (select count(*) from vms where (g->>'severity')::int >= 2)
  + (select count(*) from tcls where code = -1)
  + (select count(*) from latest where extract(epoch from now()) - (doc->>'generated')::numeric > 7200)
  + case when (select count(*) from latest) = 0 then 1000 else 0 end
)::int as "Situação" """

SQL["problemas"] = BASE + TESTS_CTE + ("""select "VM", "Host", "Problema", "Último backup OK", "Detalhe" from (
  select g->>'name' as "VM", host as "Host",
    case g->>'state'
      when 'sem_backup' then 'NUNCA TEVE BACKUP'
      when 'stale' then 'BACKUP ATRASADO'
      when 'falhou' then 'ÚLTIMO BACKUP FALHOU'
      when 'sem_job' then 'SEM REGRA DE BACKUP'
      else g->>'state' end as "Problema",
    case when (g->>'last_backup')::numeric > 0
      then to_char(""" + BRT % "(g->>'last_backup')::numeric" + """, 'DD/MM/YYYY HH24:MI')
      else 'nunca' end as "Último backup OK",
    coalesce(nullif(g->>'last_run_msg', ''), '') as "Detalhe",
    (g->>'severity')::int as sev, (g->>'vmid')::int as vmid
  from vms where (g->>'severity')::int >= 2
  union all
  select g->>'name', host,
    case when t->>'last_level' = 'restore' then 'RESTORE DE TESTE FALHOU' else 'VERIFICAÇÃO DO BACKUP FALHOU' end,
    case when (g->>'last_backup')::numeric > 0
      then to_char(""" + BRT % "(g->>'last_backup')::numeric" + """, 'DD/MM/YYYY HH24:MI')
      else 'nunca' end,
    coalesce(nullif(t->>'last_msg', ''), ''), 2, (g->>'vmid')::int
  from tcls where code = -1
  union all
  select 'Nenhuma VM com problema', '', 'TUDO CERTO', '', '', -1, 0
  where not exists (select 1 from vms where (g->>'severity')::int >= 2)
    and not exists (select 1 from tcls where code = -1)
) p
order by sev desc, "Host", vmid
limit 10""")

SQL["ok"] = BASE + "select count(*)::int as \"VMs com backup OK\" from vms where g->>'state' = 'ok'"
SQL["aguardando"] = BASE + "select count(*)::int as \"Aguardando\" from vms where g->>'state' in ('aguardando', 'novo_sem_job')"
SQL["isentas"] = BASE + "select count(*)::int as \"Isentas\" from vms where g->>'state' = 'isento'"
SQL["joberr"] = BASE + "select coalesce(sum((l.doc->'summary'->>'job_errors_14d')::int), 0)::int as \"Jobs com erro\" from latest l"

SQL["coleta"] = BASE + """select h.host as "Host",
  (l.doc->>'version')::int as "Coletor",
  (l.doc->'summary'->>'guests')::int as "VMs",
  (l.doc->'summary'->>'problems')::int as "Com problema",
  round(((extract(epoch from now()) - (l.doc->>'generated')::numeric) / 60)::numeric, 0)::int as "Última coleta há"
from latest l
join items i on i.itemid = l.itemid
join hosts h on h.hostid = i.hostid
order by h.host"""

SQL["jobs"] = BASE + r"""select h.host as "Host",
  case
    when jj.job->>'schedule' ~ '^\d{2}:\d{2}$' then 'todo dia às ' || (jj.job->>'schedule')
    when jj.job->>'schedule' ~ '^(mon|tue|wed|thu|fri|sat|sun) \d{2}:\d{2}$' then
      (case split_part(jj.job->>'schedule', ' ', 1) when 'mon' then 'segunda' when 'tue' then 'terça' when 'wed' then 'quarta'
         when 'thu' then 'quinta' when 'fri' then 'sexta' when 'sat' then 'sábado' else 'domingo' end)
      || ' às ' || split_part(jj.job->>'schedule', ' ', 2)
    when jj.job->>'schedule' ~ '^\*-\*-\d{2} \d{2}:\d{2}$' then
      'dia ' || ltrim(substr(jj.job->>'schedule', 5, 2), '0') || ' de cada mês às ' || split_part(jj.job->>'schedule', ' ', 2)
    else jj.job->>'schedule' end as "Quando",
  coalesce((select string_agg(""" + SHORT % "vv.g->>'name'" + r""", ', ' order by (vv.g->>'vmid')::int)
            from vms vv where vv.host = h.host and vv.g->'jobs' @> to_jsonb(jj.job->>'id')),
           jj.job->>'scope') as "VMs no job",
  case when (jj.job->>'enabled')::boolean then 'sim' else 'NÃO' end as "Ativo",
  case when (jj.job->>'mailto')::boolean then 'sim' else 'NÃO' end as "Avisa por e-mail",
  left(replace(jj.job->>'id', 'backup-', ''), 8) as "ID do job"
from latest l
join items i on i.itemid = l.itemid
join hosts h on h.hostid = i.hostid
cross join lateral jsonb_array_elements(l.doc->'jobs') as jj(job)
order by h.host, jj.job->>'schedule', jj.job->>'id'"""

SQL["vms"] = BASE + TESTS_CTE + """select v.g->>'name' as "VM",
  v.host as "Host",
  v.g->>'state' as "Backup",
  case when (v.g->>'last_backup')::numeric > 0 then to_timestamp((v.g->>'last_backup')::numeric) end as "Último backup",
  """ + HUMAN_H % {"c": "v.g->>'age_h'", "d": 1} + """ as "Há",
  """ + HUMAN_H % {"c": "v.g->>'threshold_h'", "d": 0} + """ as "Alerta após",
  (v.g->>'copies')::int as "Cópias no NAS",
  (v.g->>'last_size_gb')::numeric as "Tamanho do último",
  case when tc.code is null or tc.code = 998 then 'NÃO TESTA'
       when tc.code = 999 then 'NUNCA TESTADA'
       when tc.code = -1 then 'FALHOU'
       when tc.lvl = 'I' then 'INTEGRIDADE OK'
       else 'RESTORE OK' end as "Teste do backup",
  case when tc.code between 0 and 899 then tc.code || case when tc.code = 1 then ' dia' else ' dias' end end as "Testado há"
from vms v
left join tcls tc on tc.host = v.host and tc.g->>'vmid' = v.g->>'vmid'
order by (v.g->>'severity')::int desc, case when v.g->>'state' = 'isento' then 1 else 0 end, v.host, (v.g->>'vmid')::int"""

SQL["detalhes"] = BASE + """select host as "Host",
  g->>'vmid' as "VMID",
  g->>'name' as "VM",
  g->>'status' as "Máquina",
  g->'jobs'->>0 as "Job (ID completo)",
  g->>'last_run_result' as "Última execução do job",
  case when (g->>'last_run_ts')::numeric > 0 then to_timestamp((g->>'last_run_ts')::numeric) end as "Executado em",
  nullif(g->>'last_run_msg', '') as "Mensagem do vzdump"
from vms
order by host, (g->>'vmid')::int"""


def target(sql):
    return [{"datasource": DS, "editorMode": "code", "format": "table", "rawQuery": True, "rawSql": sql, "refId": "A"}]


def vmap(items):
    """{valor: (texto, cor)} -> mapeamento de valores."""
    return [{"type": "value", "options": {k: {"text": t, "color": c, "index": i} for i, (k, (t, c)) in enumerate(items.items())}}]


def steps(*pairs):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in pairs]}


def stat(pid, title, sql, x, y, w, h, thresholds, mappings=None, unit=None, size=34):
    d = {"color": {"mode": "thresholds"}, "mappings": mappings or [], "thresholds": thresholds, "decimals": 0}
    if unit:
        d["unit"] = unit
    return {"id": pid, "type": "stat", "title": title, "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "targets": target(sql), "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""}, "colorMode": "background_solid",
                        "graphMode": "none", "justifyMode": "center", "textMode": "value", "orientation": "auto",
                        "text": {"titleSize": 14, "valueSize": size}}}


def prop(name, pid, value):
    return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": pid, "value": value}]}


def width(name, w):
    return prop(name, "custom.width", w)


def flat_color(name, mappings, base=None):
    """Celula com cor de fundo solida (sem degrade) a partir de mapeamento de valores."""
    props = [{"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}}, {"id": "mappings", "value": mappings}]
    if base:
        props.append({"id": "thresholds", "value": steps((None, base))})
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def table(pid, title, sql, x, y, w, h, overrides, no_value="-"):
    return {"id": pid, "type": "table", "title": title, "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "targets": target(sql),
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": False},
                                         "noValue": no_value, "color": {"mode": "thresholds"},
                                         "thresholds": steps((None, "text"))},
                            "overrides": overrides},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}}


def row(pid, title, y, collapsed=False, inner=None):
    return {"id": pid, "type": "row", "title": title, "collapsed": collapsed, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": inner or []}


STATE = {"ok": ("OK", OK), "isento": ("ISENTO", MUTED), "aguardando": ("AGUARDANDO", WARN), "novo_sem_job": ("NOVO, SEM JOB", WARN),
         "falhou": ("FALHOU", BAD), "stale": ("ATRASADO", BAD), "sem_backup": ("SEM BACKUP", BAD), "sem_job": ("SEM JOB", BAD)}
TESTE = {"RESTORE OK": ("RESTORE OK", OK), "INTEGRIDADE OK": ("INTEGRIDADE OK", INFO), "FALHOU": ("FALHOU", BAD),
         "NUNCA TESTADA": ("NUNCA TESTADA", WARN), "NÃO TESTA": ("NÃO TESTA", MUTED)}
RUN = {"ok": ("ok", OK), "failed": ("FALHOU", BAD), "none": ("-", MUTED), "running": ("rodando", INFO)}
SIMNAO = {"sim": ("sim", OK), "NÃO": ("NÃO", BAD)}

SITUACAO_MAP = [{"type": "value", "options": {"0": {"text": "TUDO OK", "color": OK, "index": 0}}},
                {"type": "range", "options": {"from": 1000, "to": 100000, "result": {"text": "SEM DADOS", "color": BAD, "index": 1}}}]

panels = [
    stat(1, "SITUAÇÃO GERAL", SQL["situacao"], 0, 0, 6, 4, steps((None, BAD)), SITUACAO_MAP, unit="suffix: com problema", size=30),
    table(2, "PROBLEMAS AGORA (backup e teste de restore)", SQL["problemas"], 6, 0, 18, 4,
          [flat_color("Problema", vmap({"TUDO CERTO": ("TUDO CERTO", OK)}), base=BAD), width("VM", 200), width("Host", 130),
           width("Problema", 250), width("Último backup OK", 170)], no_value="-"),
    stat(3, "VMs com backup OK", SQL["ok"], 0, 4, 6, 3, steps((None, BAD), (1, OK))),
    stat(4, "Aguardando 1º backup / novas", SQL["aguardando"], 6, 4, 6, 3, steps((None, MUTED), (1, WARN))),
    stat(5, "Isentas (sem backup de propósito)", SQL["isentas"], 12, 4, 6, 3, steps((None, MUTED))),
    stat(6, "Jobs com erro (últimos 14 dias)", SQL["joberr"], 18, 4, 6, 3, steps((None, OK), (1, BAD))),
    row(7, "HOSTS E JOBS DE BACKUP", 7),
    table(8, "Coleta por host (o monitor está vivo?)", SQL["coleta"], 0, 8, 8, 8,
          [width("Host", 130), width("Coletor", 80), width("VMs", 60), width("Com problema", 110),
           {"matcher": {"id": "byName", "options": "Com problema"},
            "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                           {"id": "thresholds", "value": steps((None, OK), (1, BAD))}]},
           {"matcher": {"id": "byName", "options": "Última coleta há"},
            "properties": [{"id": "unit", "value": "m"}, {"id": "decimals", "value": 0},
                           {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                           {"id": "thresholds", "value": steps((None, OK), (90, WARN), (120, BAD))}]}]),
    table(9, "Jobs de backup (quando rodam e quais VMs cobrem)", SQL["jobs"], 8, 8, 16, 8,
          [width("Host", 130), width("Quando", 210), width("Ativo", 80), width("Avisa por e-mail", 150), width("ID do job", 110),
           {"matcher": {"id": "byName", "options": "VMs no job"},
            "properties": [{"id": "custom.cellOptions", "value": {"type": "auto", "wrapText": True}}]},
           flat_color("Ativo", vmap(SIMNAO)), flat_color("Avisa por e-mail", vmap(SIMNAO))]),
    row(10, "TODAS AS VMs", 16),
    table(11, "Estado do backup e do teste de restore de cada VM", SQL["vms"], 0, 17, 24, 28,
          [width("VM", 200), width("Host", 130), width("Backup", 150), width("Último backup", 170), width("Há", 100),
           width("Alerta após", 110), width("Cópias no NAS", 120), width("Tamanho do último", 140), width("Teste do backup", 160),
           width("Testado há", 110),
           flat_color("Backup", vmap(STATE)), flat_color("Teste do backup", vmap(TESTE)),
           prop("Último backup", "unit", "dateTimeAsLocal"),
           prop("Tamanho do último", "unit", "decgbytes"), prop("Tamanho do último", "decimals", 1)]),
    row(12, "DETALHES TÉCNICOS (VMID, job completo, mensagem do vzdump)", 45, collapsed=True, inner=[
        table(13, "Último vzdump de cada VM", SQL["detalhes"], 0, 46, 24, 14,
              [width("Host", 130), width("VMID", 80), width("VM", 200), width("Máquina", 110), width("Última execução do job", 170),
               width("Executado em", 170), flat_color("Última execução do job", vmap(RUN)),
               prop("Executado em", "unit", "dateTimeAsLocal")])]),
]

dash = {
    "id": None, "uid": "pve-backup-status-v2", "title": "Backups Proxmox (v2)",
    "tags": ["backup", "proxmox", "zabbix", "v2"], "timezone": "browser", "editable": True,
    "schemaVersion": 39, "version": 1, "refresh": "1m", "time": {"from": "now-6h", "to": "now"},
    "timepicker": {"hidden": True},
    "annotations": {"list": []}, "templating": {"list": []}, "links": [], "panels": panels,
    "description": "Estado do backup e do teste de restore de cada VM/CT dos Proxmox (pve1, pve2, pve3), lido do JSON do pve-backup-monitor e do "
                   "testador no Zabbix. Mesmos dados do painel anterior, em layout novo.",
}

json.dump(dash, open(OUT, "w", encoding="utf-8", newline="\n"), indent=2, ensure_ascii=False)
json.dump(SQL, open(OUT + ".sql.json", "w", encoding="utf-8", newline="\n"), ensure_ascii=False)
print("ok", len(panels), "elementos")
