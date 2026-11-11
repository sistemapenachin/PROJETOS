-- ============================================================
-- Schema PostgreSQL - Cronograma de Projetos e Tarefas
-- Idempotente: pode ser executado mais de uma vez sem erro.
-- (O app.py também cria isso sozinho via db.create_all();
--  use este arquivo se preferir criar o banco manualmente.)
-- ============================================================

CREATE TABLE IF NOT EXISTS projetos (
    id           SERIAL PRIMARY KEY,
    nome         VARCHAR(150) NOT NULL,
    descricao    TEXT,
    data_inicio  DATE         NOT NULL,
    data_fim     DATE         NOT NULL,
    responsavel  VARCHAR(120),
    status       VARCHAR(30)  NOT NULL DEFAULT 'Planejada',
    CONSTRAINT ck_projetos_datas CHECK (data_fim >= data_inicio)
);

CREATE TABLE IF NOT EXISTS tarefas (
    id             SERIAL PRIMARY KEY,
    projeto_id     INTEGER      NOT NULL REFERENCES projetos(id) ON DELETE CASCADE,
    nome           VARCHAR(150) NOT NULL,
    responsavel    VARCHAR(120),
    data_inicio    DATE         NOT NULL,
    data_fim       DATE         NOT NULL,
    status         VARCHAR(30)  NOT NULL DEFAULT 'Pendente',
    progresso      INTEGER      NOT NULL DEFAULT 0,
    depende_de_id  INTEGER      REFERENCES tarefas(id) ON DELETE SET NULL,
    CONSTRAINT ck_tarefas_datas     CHECK (data_fim >= data_inicio),
    CONSTRAINT ck_tarefas_progresso CHECK (progresso BETWEEN 0 AND 100)
);

CREATE INDEX IF NOT EXISTS ix_tarefas_projeto_id    ON tarefas(projeto_id);
CREATE INDEX IF NOT EXISTS ix_tarefas_depende_de_id ON tarefas(depende_de_id);
CREATE INDEX IF NOT EXISTS ix_tarefas_data_fim      ON tarefas(data_fim);
