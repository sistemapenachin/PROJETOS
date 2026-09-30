#!/usr/bin/env python3
"""
app.py
------------------------------------------------------------
Aplicação Flask para gerenciamento de Projetos e Tarefas com
cronograma em calendário (FullCalendar.js no front-end).

Funcionalidades:
    - CRUD de Projetos
    - CRUD de Tarefas (vinculadas a um Projeto), com progresso (%)
      e dependência opcional de outra tarefa do mesmo projeto.
    - Lógica de PCP: qualquer tarefa/projeto cuja data_fim já passou
      e que não esteja "Concluído" é automaticamente marcado como
      "Atrasado" (recalculado a cada requisição relevante).
    - Campo opcional "dias_para_concluir" na criação de tarefas: o
      prazo (data_fim) é calculado automaticamente a partir do início
      (data informada, fim da predecessora + 1 dia, ou início do
      projeto) e validado contra a data_fim do projeto.
    - Download do Gantt do projeto (SVG) em /api/projetos/<id>/gantt,
      gerado no servidor; o front-end também converte para PNG.
    - Endpoint de calendário unificado, retornando eventos de
      TODAS as tarefas de TODOS os projetos, cada projeto com
      uma cor própria.
------------------------------------------------------------
"""

import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape

from flask import Flask, Response, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy

# ------------------------------------------------------------------
# Configuração básica
# ------------------------------------------------------------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///cronograma.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# "Planejada" é usado como status inicial padrão de um Projeto recém-criado.
# Os demais valores valem tanto para Projeto quanto para Tarefa.
STATUS_VALIDOS = ["Planejada", "Pendente", "Em Andamento", "Concluído", "Atrasado"]

# Paleta de cores usada para diferenciar projetos no calendário e nos
# cards da aba "Projetos". Cicla pelas cores conforme a quantidade de
# projetos cresce.
PALETA_CORES = [
    "#2563eb",  # azul
    "#0891b2",  # ciano
    "#d97706",  # laranja
    "#16a34a",  # verde
    "#7c3aed",  # roxo
    "#dc2626",  # vermelho
    "#db2777",  # rosa
    "#65a30d",  # verde-limão
    "#4b5563",  # cinza
    "#ea580c",  # laranja-escuro
]


def cor_do_projeto(projeto_id: int) -> str:
    """Retorna uma cor determinística para o projeto, baseada no seu id."""
    return PALETA_CORES[(projeto_id - 1) % len(PALETA_CORES)]


# ------------------------------------------------------------------
# Modelos
# ------------------------------------------------------------------
class Projeto(db.Model):
    __tablename__ = "projetos"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    data_inicio = db.Column(db.Date, nullable=False)
    data_fim = db.Column(db.Date, nullable=False)
    responsavel = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(30), nullable=False, default="Planejada")

    tarefas = db.relationship(
        "Tarefa",
        backref="projeto",
        cascade="all, delete-orphan",
        lazy=True,
        foreign_keys="Tarefa.projeto_id",
    )

    def to_dict(self):
        total = len(self.tarefas)
        concluidas = sum(1 for t in self.tarefas if t.status == "Concluído")
        atrasadas = sum(1 for t in self.tarefas if t.status == "Atrasado")
        progresso_medio = (
            round(sum(t.progresso for t in self.tarefas) / total) if total else 0
        )

        return {
            "id": self.id,
            "nome": self.nome,
            "descricao": self.descricao,
            "data_inicio": self.data_inicio.isoformat(),
            "data_fim": self.data_fim.isoformat(),
            "responsavel": self.responsavel,
            "status": self.status,
            "cor": cor_do_projeto(self.id),
            "total_tarefas": total,
            "tarefas_concluidas": concluidas,
            "tarefas_atrasadas": atrasadas,
            "progresso_medio": progresso_medio,
        }


class Tarefa(db.Model):
    __tablename__ = "tarefas"

    id = db.Column(db.Integer, primary_key=True)
    projeto_id = db.Column(
        db.Integer, db.ForeignKey("projetos.id"), nullable=False
    )
    nome = db.Column(db.String(150), nullable=False)
    responsavel = db.Column(db.String(120), nullable=True)
    data_inicio = db.Column(db.Date, nullable=False)
    data_fim = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(30), nullable=False, default="Pendente")
    progresso = db.Column(db.Integer, nullable=False, default=0)

    # Dependência opcional de outra tarefa do mesmo projeto (predecessora).
    depende_de_id = db.Column(
        db.Integer, db.ForeignKey("tarefas.id"), nullable=True
    )
    depende_de = db.relationship(
        "Tarefa", remote_side=[id], foreign_keys=[depende_de_id]
    )

    def to_dict(self):
        return {
            "id": self.id,
            "projeto_id": self.projeto_id,
            "projeto_nome": self.projeto.nome if self.projeto else None,
            "nome": self.nome,
            "responsavel": self.responsavel,
            "data_inicio": self.data_inicio.isoformat(),
            "data_fim": self.data_fim.isoformat(),
            "status": self.status,
            "progresso": self.progresso,
            "cor": cor_do_projeto(self.projeto_id),
            "depende_de_id": self.depende_de_id,
            "depende_de_nome": self.depende_de.nome if self.depende_de else None,
        }


# ------------------------------------------------------------------
# Lógica de PCP: atualização automática de status "Atrasado"
# ------------------------------------------------------------------
def atualizar_tarefas_atrasadas():
    """
    Percorre todas as tarefas e marca como "Atrasado" qualquer uma
    cuja data_fim já tenha passado e que ainda não esteja "Concluído".
    Chamada antes de qualquer listagem/retorno relevante para manter
    o status sempre coerente com a data atual (regra de PCP).
    """
    hoje = date.today()
    tarefas = Tarefa.query.filter(
        Tarefa.data_fim < hoje, Tarefa.status != "Concluído"
    ).all()

    alterado = False
    for tarefa in tarefas:
        if tarefa.status != "Atrasado":
            tarefa.status = "Atrasado"
            alterado = True

    if alterado:
        db.session.commit()


def atualizar_projetos_atrasados():
    """
    Aplica a mesma lógica de PCP no nível de projeto: se a data_fim
    do projeto passou e ele não está "Concluído", marca como "Atrasado".
    """
    hoje = date.today()
    projetos = Projeto.query.filter(
        Projeto.data_fim < hoje, Projeto.status != "Concluído"
    ).all()

    alterado = False
    for projeto in projetos:
        if projeto.status != "Atrasado":
            projeto.status = "Atrasado"
            alterado = True

    if alterado:
        db.session.commit()


def parse_data(valor: str) -> date:
    """Converte string 'YYYY-MM-DD' em objeto date. Lança ValueError se inválida."""
    return datetime.strptime(valor, "%Y-%m-%d").date()


def parse_progresso(valor) -> int:
    """Converte o valor recebido em um inteiro de 0 a 100 (clampado)."""
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, numero))


def calcular_prazo_por_dias(proj_inicio, proj_fim, dias, data_inicio_str=None, inicio_padrao=None):
    """
    Calcula (data_inicio, data_fim) de uma tarefa a partir de "dias para concluir".

    Data de início usada, em ordem de prioridade:
        1. data_inicio_str, se informada;
        2. inicio_padrao (ex.: dia seguinte ao fim da tarefa predecessora);
        3. data_inicio do projeto.

    O primeiro dia conta como dia de trabalho: data_fim = inicio + dias - 1.
    O prazo não pode ultrapassar a data_fim do projeto.
    Retorna (data_inicio, data_fim, erro); erro é None quando tudo certo.
    """
    try:
        dias = int(dias)
    except (TypeError, ValueError):
        return None, None, "\"Dias para concluir\" deve ser um número inteiro."
    if dias < 1:
        return None, None, "\"Dias para concluir\" deve ser de pelo menos 1 dia."

    if data_inicio_str:
        try:
            inicio = parse_data(data_inicio_str)
        except ValueError:
            return None, None, "Formato de data inválido. Use YYYY-MM-DD."
    else:
        inicio = inicio_padrao or proj_inicio

    fim = inicio + timedelta(days=dias - 1)
    if fim > proj_fim:
        return None, None, (
            f"O prazo calculado ({fim.strftime('%d/%m/%Y')}) ultrapassa o fim do "
            f"projeto ({proj_fim.strftime('%d/%m/%Y')}). Reduza os dias para concluir "
            f"ou ajuste a data de início."
        )
    return inicio, fim, None


# ------------------------------------------------------------------
# Rota principal (interface)
# ------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ------------------------------------------------------------------
# API - Projetos (CRUD)
# ------------------------------------------------------------------
@app.route("/api/projetos", methods=["GET"])
def listar_projetos():
    atualizar_projetos_atrasados()
    atualizar_tarefas_atrasadas()
    projetos = Projeto.query.order_by(Projeto.data_inicio).all()
    return jsonify([p.to_dict() for p in projetos])


@app.route("/api/projetos/<int:projeto_id>", methods=["GET"])
def obter_projeto(projeto_id):
    projeto = Projeto.query.get_or_404(projeto_id)
    return jsonify(projeto.to_dict())


@app.route("/api/projetos", methods=["POST"])
def criar_projeto():
    """
    Cria um novo Projeto. Opcionalmente aceita uma lista `tarefas`
    no corpo da requisição para criar Tarefas já vinculadas a este
    projeto na mesma operação, por exemplo:

        {
          "nome": "Projeto X",
          "data_inicio": "2026-01-01",
          "data_fim": "2026-03-01",
          "tarefas": [
            {"nome": "Tarefa 1", "data_inicio": "2026-01-01", "data_fim": "2026-01-10"},
            {"nome": "Tarefa 2", "data_inicio": "2026-01-11", "data_fim": "2026-01-20", "responsavel": "Ana", "progresso": 40}
          ]
        }

    Todas as tarefas são validadas antes de qualquer gravação: se
    alguma for inválida, nada é criado (nem o projeto) e um erro
    descritivo é retornado.
    """
    dados = request.get_json(force=True) or {}

    campos_obrigatorios = ["nome", "data_inicio", "data_fim"]
    faltando = [c for c in campos_obrigatorios if not dados.get(c)]
    if faltando:
        return jsonify(
            {"erro": f"Campos obrigatórios ausentes: {', '.join(faltando)}"}
        ), 400

    try:
        data_inicio = parse_data(dados["data_inicio"])
        data_fim = parse_data(dados["data_fim"])
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    if data_fim < data_inicio:
        return jsonify({"erro": "data_fim não pode ser anterior a data_inicio."}), 400

    status = dados.get("status", "Planejada")
    if status not in STATUS_VALIDOS:
        status = "Planejada"

    # --------------------------------------------------------------
    # Validação prévia das tarefas iniciais (se houver), antes de
    # persistir qualquer coisa no banco de dados.
    # --------------------------------------------------------------
    tarefas_entrada = dados.get("tarefas") or []
    if not isinstance(tarefas_entrada, list):
        return jsonify({"erro": "'tarefas' deve ser uma lista de tarefas."}), 400

    tarefas_validadas = []
    for idx, t in enumerate(tarefas_entrada, start=1):
        if not isinstance(t, dict):
            return jsonify({"erro": f"Tarefa #{idx} inválida."}), 400

        nome_tarefa = (t.get("nome") or "").strip()
        if not nome_tarefa:
            return jsonify({"erro": f"Tarefa #{idx}: nome é obrigatório."}), 400

        t_dias = t.get("dias_para_concluir")
        if t_dias not in (None, ""):
            # Prazo calculado automaticamente a partir dos dias informados
            # e das datas do projeto que está sendo criado.
            t_data_inicio, t_data_fim, erro = calcular_prazo_por_dias(
                data_inicio, data_fim, t_dias, t.get("data_inicio")
            )
            if erro:
                return jsonify({"erro": f"Tarefa #{idx}: {erro}"}), 400
        else:
            if not t.get("data_inicio") or not t.get("data_fim"):
                return jsonify(
                    {"erro": f"Tarefa #{idx}: informe os dias para concluir ou data_inicio e data_fim."}
                ), 400

            try:
                t_data_inicio = parse_data(t["data_inicio"])
                t_data_fim = parse_data(t["data_fim"])
            except ValueError:
                return jsonify(
                    {"erro": f"Tarefa #{idx}: formato de data inválido. Use YYYY-MM-DD."}
                ), 400

            if t_data_fim < t_data_inicio:
                return jsonify(
                    {"erro": f"Tarefa #{idx}: data_fim não pode ser anterior a data_inicio."}
                ), 400

        t_status = t.get("status", "Pendente")
        if t_status not in STATUS_VALIDOS:
            t_status = "Pendente"

        # Regra de PCP também vale para tarefas criadas junto do projeto:
        # se já nascem com data_fim vencida e não concluídas, entram
        # diretamente como "Atrasado".
        if t_status != "Concluído" and t_data_fim < date.today():
            t_status = "Atrasado"

        t_progresso = parse_progresso(t.get("progresso", 0))
        if t_status == "Concluído":
            t_progresso = 100

        tarefas_validadas.append(
            {
                "nome": nome_tarefa,
                "responsavel": t.get("responsavel"),
                "data_inicio": t_data_inicio,
                "data_fim": t_data_fim,
                "status": t_status,
                "progresso": t_progresso,
            }
        )

    # --------------------------------------------------------------
    # Persistência: projeto + tarefas (se houver) na mesma transação.
    # Dependências entre tarefas iniciais não são suportadas nesta
    # etapa (use a edição de tarefa depois de criadas para isso).
    # --------------------------------------------------------------
    projeto = Projeto(
        nome=dados["nome"],
        descricao=dados.get("descricao"),
        data_inicio=data_inicio,
        data_fim=data_fim,
        responsavel=dados.get("responsavel"),
        status=status,
    )
    db.session.add(projeto)
    db.session.flush()  # garante projeto.id disponível para as tarefas

    for t in tarefas_validadas:
        db.session.add(Tarefa(projeto_id=projeto.id, **t))

    db.session.commit()

    resposta = projeto.to_dict()
    resposta["tarefas"] = [t.to_dict() for t in projeto.tarefas]
    return jsonify(resposta), 201


@app.route("/api/projetos/<int:projeto_id>", methods=["PUT"])
def atualizar_projeto(projeto_id):
    projeto = Projeto.query.get_or_404(projeto_id)
    dados = request.get_json(force=True) or {}

    if "nome" in dados:
        projeto.nome = dados["nome"]
    if "descricao" in dados:
        projeto.descricao = dados["descricao"]
    if "responsavel" in dados:
        projeto.responsavel = dados["responsavel"]
    if "status" in dados and dados["status"] in STATUS_VALIDOS:
        projeto.status = dados["status"]

    try:
        if "data_inicio" in dados and dados["data_inicio"]:
            projeto.data_inicio = parse_data(dados["data_inicio"])
        if "data_fim" in dados and dados["data_fim"]:
            projeto.data_fim = parse_data(dados["data_fim"])
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    if projeto.data_fim < projeto.data_inicio:
        return jsonify({"erro": "data_fim não pode ser anterior a data_inicio."}), 400

    db.session.commit()
    return jsonify(projeto.to_dict())


@app.route("/api/projetos/<int:projeto_id>", methods=["DELETE"])
def excluir_projeto(projeto_id):
    projeto = Projeto.query.get_or_404(projeto_id)
    db.session.delete(projeto)  # cascade remove as tarefas do projeto
    db.session.commit()
    return jsonify({"mensagem": "Projeto excluído com sucesso."})


# ------------------------------------------------------------------
# API - Tarefas (CRUD)
# ------------------------------------------------------------------
@app.route("/api/tarefas", methods=["GET"])
def listar_tarefas():
    atualizar_tarefas_atrasadas()

    projeto_id = request.args.get("projeto_id", type=int)
    query = Tarefa.query
    if projeto_id:
        query = query.filter_by(projeto_id=projeto_id)

    tarefas = query.order_by(Tarefa.data_inicio).all()
    return jsonify([t.to_dict() for t in tarefas])


@app.route("/api/tarefas/<int:tarefa_id>", methods=["GET"])
def obter_tarefa(tarefa_id):
    tarefa = Tarefa.query.get_or_404(tarefa_id)
    return jsonify(tarefa.to_dict())


def _validar_dependencia(projeto_id, depende_de_id, tarefa_atual_id=None):
    """
    Garante que depende_de_id, se informado, aponta para uma tarefa
    existente, do mesmo projeto, e diferente da própria tarefa (evita
    dependência circular direta). Retorna (ok, erro).
    """
    if depende_de_id in (None, "", 0):
        return True, None

    try:
        depende_de_id = int(depende_de_id)
    except (TypeError, ValueError):
        return False, "depende_de_id inválido."

    if tarefa_atual_id is not None and depende_de_id == tarefa_atual_id:
        return False, "Uma tarefa não pode depender dela mesma."

    predecessora = Tarefa.query.get(depende_de_id)
    if not predecessora or predecessora.projeto_id != projeto_id:
        return False, "Tarefa predecessora inválida (deve pertencer ao mesmo projeto)."

    return True, None


@app.route("/api/tarefas", methods=["POST"])
def criar_tarefa():
    dados = request.get_json(force=True) or {}

    dias = dados.get("dias_para_concluir")
    usa_dias = dias not in (None, "")

    # Com "dias para concluir", as datas deixam de ser obrigatórias:
    # o prazo é calculado a partir do cronograma do projeto.
    campos_obrigatorios = ["projeto_id", "nome"]
    if not usa_dias:
        campos_obrigatorios += ["data_inicio", "data_fim"]
    faltando = [c for c in campos_obrigatorios if not dados.get(c)]
    if faltando:
        return jsonify(
            {"erro": f"Campos obrigatórios ausentes: {', '.join(faltando)}"}
        ), 400

    projeto = Projeto.query.get(dados["projeto_id"])
    if not projeto:
        return jsonify({"erro": "Projeto não encontrado."}), 404

    depende_de_id = dados.get("depende_de_id")
    ok, erro = _validar_dependencia(projeto.id, depende_de_id)
    if not ok:
        return jsonify({"erro": erro}), 400

    if usa_dias:
        # Sem data de início informada: começa no dia seguinte ao fim da
        # predecessora (se houver) ou, caso contrário, no início do projeto.
        inicio_padrao = None
        if depende_de_id not in (None, "", 0):
            predecessora = Tarefa.query.get(int(depende_de_id))
            inicio_padrao = predecessora.data_fim + timedelta(days=1)

        data_inicio, data_fim, erro = calcular_prazo_por_dias(
            projeto.data_inicio,
            projeto.data_fim,
            dias,
            dados.get("data_inicio"),
            inicio_padrao,
        )
        if erro:
            return jsonify({"erro": erro}), 400
    else:
        try:
            data_inicio = parse_data(dados["data_inicio"])
            data_fim = parse_data(dados["data_fim"])
        except ValueError:
            return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

        if data_fim < data_inicio:
            return jsonify({"erro": "data_fim não pode ser anterior a data_inicio."}), 400

    status = dados.get("status", "Pendente")
    if status not in STATUS_VALIDOS:
        status = "Pendente"

    # Regra de PCP também na criação: se já nasce com data_fim vencida
    # e não está concluída, entra direto como Atrasado.
    if status != "Concluído" and data_fim < date.today():
        status = "Atrasado"

    progresso = parse_progresso(dados.get("progresso", 0))
    if status == "Concluído":
        progresso = 100

    tarefa = Tarefa(
        projeto_id=dados["projeto_id"],
        nome=dados["nome"],
        responsavel=dados.get("responsavel"),
        data_inicio=data_inicio,
        data_fim=data_fim,
        status=status,
        progresso=progresso,
        depende_de_id=depende_de_id or None,
    )
    db.session.add(tarefa)
    db.session.commit()

    return jsonify(tarefa.to_dict()), 201


@app.route("/api/tarefas/<int:tarefa_id>", methods=["PUT"])
def atualizar_tarefa(tarefa_id):
    tarefa = Tarefa.query.get_or_404(tarefa_id)
    dados = request.get_json(force=True) or {}

    if "projeto_id" in dados and dados["projeto_id"]:
        projeto = Projeto.query.get(dados["projeto_id"])
        if not projeto:
            return jsonify({"erro": "Projeto não encontrado."}), 404
        tarefa.projeto_id = dados["projeto_id"]

    if "nome" in dados:
        tarefa.nome = dados["nome"]
    if "responsavel" in dados:
        tarefa.responsavel = dados["responsavel"]
    if "status" in dados and dados["status"] in STATUS_VALIDOS:
        tarefa.status = dados["status"]
    if "progresso" in dados:
        tarefa.progresso = parse_progresso(dados["progresso"])

    if "depende_de_id" in dados:
        depende_de_id = dados["depende_de_id"]
        ok, erro = _validar_dependencia(
            tarefa.projeto_id, depende_de_id, tarefa_atual_id=tarefa.id
        )
        if not ok:
            return jsonify({"erro": erro}), 400
        tarefa.depende_de_id = depende_de_id or None

    try:
        if "data_inicio" in dados and dados["data_inicio"]:
            tarefa.data_inicio = parse_data(dados["data_inicio"])
        if "data_fim" in dados and dados["data_fim"]:
            tarefa.data_fim = parse_data(dados["data_fim"])
    except ValueError:
        return jsonify({"erro": "Formato de data inválido. Use YYYY-MM-DD."}), 400

    # "Dias para concluir" (opcional): recalcula o prazo a partir da data
    # de início da tarefa, respeitando o fim do projeto.
    dias = dados.get("dias_para_concluir")
    if dias not in (None, ""):
        projeto = Projeto.query.get(tarefa.projeto_id)
        inicio, fim, erro = calcular_prazo_por_dias(
            projeto.data_inicio, projeto.data_fim, dias,
            inicio_padrao=tarefa.data_inicio,
        )
        if erro:
            return jsonify({"erro": erro}), 400
        tarefa.data_inicio, tarefa.data_fim = inicio, fim

    if tarefa.data_fim < tarefa.data_inicio:
        return jsonify({"erro": "data_fim não pode ser anterior a data_inicio."}), 400

    # Reaplica a regra de PCP após a edição
    if tarefa.status != "Concluído" and tarefa.data_fim < date.today():
        tarefa.status = "Atrasado"

    if tarefa.status == "Concluído":
        tarefa.progresso = 100

    db.session.commit()
    return jsonify(tarefa.to_dict())


@app.route("/api/tarefas/<int:tarefa_id>", methods=["DELETE"])
def excluir_tarefa(tarefa_id):
    tarefa = Tarefa.query.get_or_404(tarefa_id)
    # Remove a referência de dependência em tarefas que apontavam para esta.
    Tarefa.query.filter_by(depende_de_id=tarefa.id).update({"depende_de_id": None})
    db.session.delete(tarefa)
    db.session.commit()
    return jsonify({"mensagem": "Tarefa excluída com sucesso."})


# ------------------------------------------------------------------
# API - Gantt do projeto (download)
# ------------------------------------------------------------------
MESES_PT = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
            "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def _slug(texto: str) -> str:
    """Nome de arquivo seguro (ASCII) a partir do nome do projeto."""
    norm = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", norm).strip("-").lower() or "projeto"


def _cortar(texto: str, limite: int) -> str:
    texto = texto or ""
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def gerar_gantt_svg(projeto, tarefas) -> str:
    """
    Gera um SVG autocontido (sem CSS/fontes externas) com o Gantt das
    tarefas do projeto: barras na cor do projeto, progresso, dependências,
    linha de "hoje" e escala mensal/semanal.
    """
    cor = cor_do_projeto(projeto.id)
    hoje = date.today()

    inicio = min([projeto.data_inicio] + [t.data_inicio for t in tarefas])
    fim = max([projeto.data_fim] + [t.data_fim for t in tarefas])
    total_dias = (fim - inicio).days + 1

    ESQ, DIR = 280, 190
    ROW_H, BAR_H = 34, 18
    TOPO_TIMELINE = 64
    Y0 = 110  # início das linhas de tarefas

    ppd = max(4.0, min(32.0, 860.0 / total_dias))  # pixels por dia
    largura_tl = total_dias * ppd
    W = int(ESQ + largura_tl + DIR)
    H = int(Y0 + len(tarefas) * ROW_H + 40)

    def x(d):
        return ESQ + (d - inicio).days * ppd

    out = []
    a = out.append
    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Arial, Helvetica, sans-serif">')
    a('<defs><marker id="seta" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">'
      '<path d="M0,0 L7,3.5 L0,7 z" fill="#94a3b8"/></marker></defs>')
    a(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')

    # Título
    a(f'<rect x="0" y="0" width="{W}" height="6" fill="{cor}"/>')
    a(f'<text x="16" y="30" font-size="18" font-weight="bold" fill="#0f172a">{escape(_cortar(projeto.nome, 80))}</text>')
    sub = f"Período: {projeto.data_inicio.strftime('%d/%m/%Y')} a {projeto.data_fim.strftime('%d/%m/%Y')}"
    if projeto.responsavel:
        sub += f"  ·  {projeto.responsavel}"
    a(f'<text x="16" y="50" font-size="12" fill="#64748b">{escape(sub)}</text>')

    # Cabeçalho: meses
    cursor = inicio
    alt = 0
    while cursor <= fim:
        prox_mes = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        seg_fim = min(fim, prox_mes - timedelta(days=1))
        x0 = x(cursor)
        w = ((seg_fim - cursor).days + 1) * ppd
        a(f'<rect x="{x0:.1f}" y="{TOPO_TIMELINE}" width="{w:.1f}" height="22" fill="{"#f1f5f9" if alt % 2 == 0 else "#e2e8f0"}"/>')
        if w >= 46:
            a(f'<text x="{x0 + 6:.1f}" y="{TOPO_TIMELINE + 15}" font-size="11" font-weight="bold" fill="#334155">{MESES_PT[cursor.month - 1]}/{cursor.year}</text>')
        alt += 1
        cursor = prox_mes

    # Faixas de linha + grade semanal
    for i in range(len(tarefas)):
        if i % 2 == 0:
            a(f'<rect x="0" y="{Y0 + i * ROW_H}" width="{W}" height="{ROW_H}" fill="#f8fafc"/>')
    a(f'<rect x="0" y="{TOPO_TIMELINE}" width="{ESQ}" height="22" fill="#f1f5f9"/>')
    a(f'<text x="16" y="{TOPO_TIMELINE + 15}" font-size="11" font-weight="bold" fill="#334155">Tarefa</text>')

    d = inicio
    while d <= fim:
        if d.weekday() == 0:  # segundas-feiras
            xd = x(d)
            a(f'<line x1="{xd:.1f}" y1="{TOPO_TIMELINE + 22}" x2="{xd:.1f}" y2="{Y0 + len(tarefas) * ROW_H}" stroke="#e2e8f0" stroke-width="1"/>')
            if ppd * 7 >= 30:
                a(f'<text x="{xd + 3:.1f}" y="{TOPO_TIMELINE + 36}" font-size="9" fill="#94a3b8">{d.day:02d}</text>')
        d += timedelta(days=1)
    a(f'<line x1="{ESQ}" y1="{TOPO_TIMELINE}" x2="{ESQ}" y2="{Y0 + len(tarefas) * ROW_H}" stroke="#cbd5e1" stroke-width="1"/>')

    # Linhas: rótulo + barra
    posicoes = {}
    for i, t in enumerate(tarefas):
        y = Y0 + i * ROW_H
        ymid = y + ROW_H / 2
        bx = x(t.data_inicio)
        bw = ((t.data_fim - t.data_inicio).days + 1) * ppd
        by = ymid - BAR_H / 2
        posicoes[t.id] = (bx, bx + bw, ymid)

        a(f'<text x="16" y="{y + 15}" font-size="12" font-weight="bold" fill="#1e293b">{escape(_cortar(t.nome, 38))}</text>')
        detalhe = t.status + (f" · {t.responsavel}" if t.responsavel else "")
        cor_det = "#b91c1c" if t.status == "Atrasado" else "#64748b"
        a(f'<text x="16" y="{y + 28}" font-size="10" fill="{cor_det}">{escape(_cortar(detalhe, 46))}</text>')

        a(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{BAR_H}" rx="4" fill="{cor}" fill-opacity="0.35"/>')
        if t.progresso > 0:
            a(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw * t.progresso / 100:.1f}" height="{BAR_H}" rx="4" fill="{cor}"/>')
        rotulo = f"{t.data_inicio.strftime('%d/%m')} – {t.data_fim.strftime('%d/%m')} · {t.progresso}%"
        a(f'<text x="{bx + bw + 6:.1f}" y="{ymid + 4:.1f}" font-size="11" fill="#475569">{rotulo}</text>')

    # Dependências (setas em cotovelo)
    for t in tarefas:
        if t.depende_de_id in posicoes:
            px1, px2, py = posicoes[t.depende_de_id]
            dx1, _, dy = posicoes[t.id]
            meio = px2 + 8
            a(f'<path d="M{px2:.1f},{py:.1f} H{meio:.1f} V{dy:.1f} H{dx1 - 2:.1f}" fill="none" stroke="#94a3b8" stroke-width="1.3" marker-end="url(#seta)"/>')

    # Linha de hoje
    if inicio <= hoje <= fim:
        xh = x(hoje) + ppd / 2
        a(f'<line x1="{xh:.1f}" y1="{TOPO_TIMELINE}" x2="{xh:.1f}" y2="{Y0 + len(tarefas) * ROW_H}" stroke="#dc2626" stroke-width="1.5" stroke-dasharray="4,3"/>')
        a(f'<text x="{xh + 4:.1f}" y="{TOPO_TIMELINE - 4}" font-size="10" fill="#dc2626">Hoje</text>')

    a(f'<text x="16" y="{H - 14}" font-size="10" fill="#94a3b8">Gerado em {hoje.strftime("%d/%m/%Y")} · barra escura = progresso concluído</text>')
    a('</svg>')
    return "\n".join(out)


@app.route("/api/projetos/<int:projeto_id>/gantt", methods=["GET"])
def gantt_projeto(projeto_id):
    """
    Retorna o Gantt do projeto como SVG. Com ?download=1 força o download
    do arquivo (Content-Disposition: attachment).
    """
    atualizar_tarefas_atrasadas()
    projeto = Projeto.query.get_or_404(projeto_id)
    tarefas = (
        Tarefa.query.filter_by(projeto_id=projeto_id)
        .order_by(Tarefa.data_inicio, Tarefa.id)
        .all()
    )
    if not tarefas:
        return jsonify({"erro": "Este projeto ainda não possui tarefas para gerar o Gantt."}), 400

    resp = Response(gerar_gantt_svg(projeto, tarefas), mimetype="image/svg+xml")
    if request.args.get("download"):
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="gantt-{_slug(projeto.nome)}.svg"'
        )
    return resp


# ------------------------------------------------------------------
# API - Calendário unificado (FullCalendar.js)
# ------------------------------------------------------------------
@app.route("/api/calendario", methods=["GET"])
def calendario_eventos():
    """
    Retorna todas as tarefas de todos os projetos já formatadas
    no esquema de eventos esperado pelo FullCalendar.js, com cor
    diferenciada por projeto.
    """
    atualizar_tarefas_atrasadas()

    tarefas = Tarefa.query.order_by(Tarefa.data_inicio).all()

    eventos = []
    for t in tarefas:
        # FullCalendar trata 'end' como exclusivo para eventos de dia
        # inteiro, então somamos 1 dia para incluir o último dia da tarefa.
        fim_exclusivo = t.data_fim + timedelta(days=1)

        eventos.append(
            {
                "id": t.id,
                "title": f"[{t.projeto.nome}] {t.nome}",
                "start": t.data_inicio.isoformat(),
                "end": fim_exclusivo.isoformat(),
                "allDay": True,
                "backgroundColor": cor_do_projeto(t.projeto_id),
                "borderColor": cor_do_projeto(t.projeto_id),
                "extendedProps": {
                    "projeto_id": t.projeto_id,
                    "projeto_nome": t.projeto.nome,
                    "responsavel": t.responsavel,
                    "status": t.status,
                    "progresso": t.progresso,
                },
            }
        )

    return jsonify(eventos)


# ------------------------------------------------------------------
# Inicialização do banco de dados
# ------------------------------------------------------------------
from sqlalchemy import text

def migrar_colunas_faltantes():
    """Garante que as novas colunas existam na tabela tarefas."""
    with db.engine.connect() as conn:
        # Adiciona progresso caso nao exista
        try:
            conn.execute(text("ALTER TABLE tarefas ADD COLUMN progresso INTEGER DEFAULT 0"))
            conn.commit()
        except Exception:
            pass

        # Adiciona depende_de_id caso nao exista
        try:
            conn.execute(text("ALTER TABLE tarefas ADD COLUMN depende_de_id INTEGER REFERENCES tarefas(id)"))
            conn.commit()
        except Exception:
            pass


def inicializar_banco():
    with app.app_context():
        db.create_all()
        migrar_colunas_faltantes()
        atualizar_projetos_atrasados()
        atualizar_tarefas_atrasadas()


if __name__ == "__main__":
    inicializar_banco()
    app.run(debug=True)