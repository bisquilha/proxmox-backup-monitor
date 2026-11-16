#!/bin/bash
# Roda no host onde ficam o Zabbix e o Grafana em Docker.
# Uso: GRAFANA_AUTH='admin:<senha>' ./setup-datasource.sh
# Cria um usuario SOMENTE LEITURA no banco do Zabbix (acesso a 3 tabelas) e cadastra no Grafana
# o datasource "Zabbix DB (somente leitura)" com o uid zbx-db-ro, usado pelo painel de backups.
# Pode rodar de novo: recria o usuario/senha e o datasource.
GAUTH="${GRAFANA_AUTH:?defina GRAFANA_AUTH=usuario:senha do admin do Grafana}"
DB=zabbix_stack-zabbix-db-1
DBU=$(docker exec "$DB" printenv POSTGRES_USER)
DBN=$(docker exec "$DB" printenv POSTGRES_DB); DBN=${DBN:-$DBU}
PW=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)

docker exec -i "$DB" psql -v ON_ERROR_STOP=1 -U "$DBU" -d "$DBN" <<SQL
DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN CREATE ROLE grafana_ro LOGIN; END IF; END \$\$;
ALTER ROLE grafana_ro PASSWORD '$PW';
GRANT CONNECT ON DATABASE "$DBN" TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON hosts, items, history_text TO grafana_ro;
SQL

curl -s -u "$GAUTH" -X DELETE http://localhost:3000/api/datasources/uid/zbx-db-ro > /dev/null
curl -s -u "$GAUTH" -H 'Content-Type: application/json' -X POST http://localhost:3000/api/datasources -d "{\"uid\":\"zbx-db-ro\",\"name\":\"Zabbix DB (somente leitura)\",\"type\":\"grafana-postgresql-datasource\",\"access\":\"proxy\",\"url\":\"zabbix-db:5432\",\"user\":\"grafana_ro\",\"database\":\"$DBN\",\"jsonData\":{\"database\":\"$DBN\",\"sslmode\":\"disable\",\"postgresVersion\":1600},\"secureJsonData\":{\"password\":\"$PW\"}}"
echo
curl -s -u "$GAUTH" http://localhost:3000/api/datasources/uid/zbx-db-ro/health
echo
