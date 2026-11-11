# Deploy no Railway com PostgreSQL

## Estrutura do repositório
```
app.py
requirements.txt
Procfile
schema.sql          (opcional)
templates/index.html
```

## Passo a passo
1. Suba esta pasta para um repositório no GitHub.
2. No Railway: **New Project → Deploy from GitHub repo** e escolha o repositório.
3. No mesmo projeto: **+ New → Database → Add PostgreSQL**.
4. No serviço do app (não no Postgres) abra **Variables → New Variable → Add Reference**
   e crie `DATABASE_URL` apontando para `${{Postgres.DATABASE_URL}}`
   (se o seu serviço de banco tiver outro nome, troque `Postgres` por ele).
5. Faça o redeploy. Na primeira execução o `app.py` cria as tabelas sozinho.
6. Em **Settings → Networking → Generate Domain** para obter a URL pública.

## Criar as tabelas manualmente (opcional)
Na aba **Data** do Postgres no Railway, ou via `psql "$DATABASE_URL" -f schema.sql`.

## Teste local com Postgres
```
export DATABASE_URL="postgresql://usuario:senha@localhost:5432/cronograma"
pip install -r requirements.txt
python app.py
```
Sem `DATABASE_URL`, o app usa SQLite (`cronograma.db`) como antes.
