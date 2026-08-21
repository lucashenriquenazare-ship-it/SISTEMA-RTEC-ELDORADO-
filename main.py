"""
Equipamentos - Lançamento Remoto
Sistema web para RTEC Tratores e Eldorado Serviços lançarem despesas de
equipamentos remotamente, com login individual por pessoa.

Tudo em um único arquivo de propósito - nenhuma subpasta é necessária, o que
torna o deploy à prova de erro de upload (arrastar pastas para o GitHub pelo
navegador nem sempre preserva a estrutura de diretórios).
"""
import base64
import csv
import datetime
import gzip
import hashlib
import hmac
import io
import json
import os
from typing import Optional

from fastapi import FastAPI, Depends, Request, Form, HTTPException, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime, Date,
    ForeignKey, Text, func, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
from jinja2 import Environment, DictLoader, select_autoescape

# =====================================================================
# Configuração
# =====================================================================
SECRET_KEY = os.environ.get("SECRET_KEY", "troque-esta-chave-antes-de-publicar")
INVITE_CODES = {
    "RTEC TRATORES": os.environ.get("INVITE_CODE_RTEC", "RTEC2026"),
    "ELDORADO SERVIÇOS": os.environ.get("INVITE_CODE_ELDORADO", "ELDORADO2026"),
}

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./equipamentos.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Token para acionar a importação única dos lançamentos reais extraídos da
# planilha (rota /admin/importar-planilha). Sem essa variável configurada no
# Render, a rota fica desativada.
IMPORT_TOKEN = os.environ.get("IMPORT_TOKEN", "")

EMPRESAS = ["RTEC TRATORES", "ELDORADO SERVIÇOS"]
CATEGORIAS = ["MÃO DE OBRA", "MANUTENÇÃO", "COMBUSTÍVEL", "OUTRAS DESPESAS"]

# Login "de sistema" usado para atribuir os lançamentos importados da
# planilha original (eles não pertencem a nenhuma pessoa específica).
IMPORT_USUARIOS = {
    "RTEC TRATORES": "importacao.rtec",
    "ELDORADO SERVIÇOS": "importacao.eldorado",
}

# Na planilha original, cada equipamento é faturado (cobrado do cliente) por
# uma das duas empresas - é essa empresa que "recebe" o faturamento dele no
# acerto mensal (independente de quem pagou as despesas daquele equipamento,
# isso as duas podem fazer em qualquer máquina). Mapeamento copiado da divisão
# real da aba RESULTADO (18 equipamentos faturados pela RTEC, 5 pela Eldorado).
EQUIPAMENTOS_FATURAMENTO_EMPRESA = {
    "CARREGADEIRA L60F": "RTEC TRATORES",
    "ESCAVADEIRA VOLVO 21TON 05": "RTEC TRATORES",
    "CAMINHÃO BROOK": "RTEC TRATORES",
    "MOTONIVELADORA": "RTEC TRATORES",
    "ROLO COMPACTADOR": "RTEC TRATORES",
    "RETRO ESCAVADEIRA 02": "RTEC TRATORES",
    "CAMINHÃO MUNCK TOCO": "RTEC TRATORES",
    "ESCAVADEIRA HYUNDAI": "RTEC TRATORES",
    "RETRO ESCAVADEIRA 10": "RTEC TRATORES",
    "ESCAVADEIRA VOLVO 21TON 01": "RTEC TRATORES",
    "ESCAVADEIRA VOLVO 21TON 02": "RTEC TRATORES",
    "CAMINHÃO MUNCK 10TON": "RTEC TRATORES",
    "RETRO ESCAVADEIRA 12": "RTEC TRATORES",
    "RETRO ESCAVADEIRA 04": "RTEC TRATORES",
    "RETRO ESCAVADEIRA 03": "RTEC TRATORES",
    "RETRO ESCAVADEIRA 9": "RTEC TRATORES",
    "CAMINHÃO PIPA TRUCK": "RTEC TRATORES",
    "CAMINHÃO 3/4 COM MÓDULO": "RTEC TRATORES",
    "RETRO ESCAVADEIRA 01": "ELDORADO SERVIÇOS",
    "RETRO ESCAVADEIRA 14": "ELDORADO SERVIÇOS",
    "MINI CARREGADEIRA": "ELDORADO SERVIÇOS",
    "BROOK HEH7487": "ELDORADO SERVIÇOS",
    "MINI RETROESCAVADEIRA NOVA": "ELDORADO SERVIÇOS",
}

# Faturamento real de julho/2026 (coluna VL. FATURADO da aba RESULTADO),
# usado só na importação inicial via /admin/importar-planilha.
FATURAMENTO_PLANILHA_JUL26 = {
    "CARREGADEIRA L60F": 47900,
    "ESCAVADEIRA VOLVO 21TON 05": 44010,
    "CAMINHÃO BROOK": 20305,
    "MOTONIVELADORA": 39250,
    "ROLO COMPACTADOR": 36000,
    "RETRO ESCAVADEIRA 02": 0,
    "CAMINHÃO MUNCK TOCO": 26000,
    "ESCAVADEIRA HYUNDAI": 52170,
    "RETRO ESCAVADEIRA 10": 20764,
    "ESCAVADEIRA VOLVO 21TON 01": 0,
    "ESCAVADEIRA VOLVO 21TON 02": 0,
    "CAMINHÃO MUNCK 10TON": 24734,
    "RETRO ESCAVADEIRA 12": 19200,
    "RETRO ESCAVADEIRA 04": 19407,
    "RETRO ESCAVADEIRA 03": 19407,
    "RETRO ESCAVADEIRA 9": 22351,
    "CAMINHÃO PIPA TRUCK": 23700,
    "CAMINHÃO 3/4 COM MÓDULO": 12800,
    "RETRO ESCAVADEIRA 01": 14800,
    "RETRO ESCAVADEIRA 14": 19361.67,
    "MINI CARREGADEIRA": 18500,
    "BROOK HEH7487": 19200,
    "MINI RETROESCAVADEIRA NOVA": 18500,
}

# Itens soltos que aparecem embaixo da aba RESULTADO (linhas 36-56), fora de
# qualquer fórmula que soma no total oficial (I33/F35/I35/F38/I38 - conferido
# célula a célula, nenhum desses cai ali). São conferências/avisos: diesel,
# juros de financiamento de equipamento, resultado de outros imóveis (BMS),
# parcela etc. Por não terem fórmula de soma ligada ao Resultado, entram só
# como Observação (referência), nunca no cálculo do acerto entre as empresas.
# Itens de OUTROS NEGÓCIOS (RL, RL SUDESTE, RTEC SUDESTE, GG, básculas, MOTO,
# PIPA - colunas E a H da planilha) ficam de fora até deste registro, por
# serem de outra sociedade, sem relação com RTEC Tratores/Eldorado Serviços.
OBSERVACOES_PLANILHA_JUL26 = [
    {"empresa": "RTEC TRATORES", "descricao": "DIESEL RTEC", "valor": 18020.16},
    {"empresa": None, "descricao": "ACERTO HIG JUL2026", "valor": 2277.05},
    {"empresa": "ELDORADO SERVIÇOS", "descricao": "DESPESAS ELDORADO (ref. aba DESPESAS da planilha)", "valor": -1612.32},
    {"empresa": "RTEC TRATORES", "descricao": "RESULTADO RTEC BMS 46 MAI SINARCO", "valor": 0},
    {"empresa": "RTEC TRATORES", "descricao": "RESULTADO RTEC BMS 17 MAI SINARCO 406", "valor": 0},
    {"empresa": "RTEC TRATORES", "descricao": "DIESEL PAGO PELA RTEC (03/07, 14/07, 21/07, 30/07)", "valor": 119600},
    {"empresa": None, "descricao": "RESULTADO QUANTUM BMS 18 (JUN)", "valor": 60200.77},
    {"empresa": None, "descricao": "DIFERENÇA RT 12 JULHO2026", "valor": 2578.27},
    {"empresa": None, "descricao": "JUROS 2% ESC EC220D - financiamento R$ 150.000,00", "valor": 3000},
    {"empresa": None, "descricao": "JUROS 2% MUNCK - financiamento R$ 135.000,00", "valor": 2700},
    {"empresa": None, "descricao": "JUROS 2% 3/4 JR - financiamento R$ 62.500,00", "valor": 1250},
    {"empresa": None, "descricao": "JUROS 2% PIPA - financiamento R$ 150.000,00", "valor": 3000},
    {"empresa": None, "descricao": "JUROS 2% ROLO - financiamento R$ 147.500,00", "valor": 2950},
    {"empresa": None, "descricao": "PARCELA L120 10/10", "valor": 5000},
    {"empresa": None, "descricao": "RESULTADO ITAMARACÁ - BMS JUL", "valor": 34956.51},
    {"empresa": None, "descricao": "GG", "valor": 6000},
]

# Copiado verbatim da aba RESULTADO da planilha original (EQUIPAMENTOS_JUL26) -
# nada foi inventado aqui.
EQUIPAMENTOS_SEED = [
    ("CARREGADEIRA L60F", "CG60"),
    ("ESCAVADEIRA VOLVO 21TON 05", "ESCAV 05"),
    ("CAMINHÃO BROOK", "KRA 7H60"),
    ("MOTONIVELADORA", "PATROL 01"),
    ("ROLO COMPACTADOR", "ROLO 01"),
    ("RETRO ESCAVADEIRA 02", "RT 02"),
    ("CAMINHÃO MUNCK TOCO", "HLN6484"),
    ("ESCAVADEIRA HYUNDAI", "ESCAV HYUNDAI"),
    ("RETRO ESCAVADEIRA 10", "RT 10"),
    ("ESCAVADEIRA VOLVO 21TON 01", "ESCAV 01"),
    ("ESCAVADEIRA VOLVO 21TON 02", "ESCAV 02"),
    ("CAMINHÃO MUNCK 10TON", "HIJ"),
    ("RETRO ESCAVADEIRA 12", "RT 12"),
    ("RETRO ESCAVADEIRA 04", "RT 04"),
    ("RETRO ESCAVADEIRA 03", "RT 03"),
    ("RETRO ESCAVADEIRA 9", "RT 09"),
    ("CAMINHÃO PIPA TRUCK", "OMB 1543"),
    ("CAMINHÃO 3/4 COM MÓDULO", "OLQ0951"),
    ("RETRO ESCAVADEIRA 01", "RT 01"),
    ("RETRO ESCAVADEIRA 14", "RT 14"),
    ("MINI CARREGADEIRA", "MINI 01"),
    ("BROOK HEH7487", "BROOK 02"),
    ("MINI RETROESCAVADEIRA NOVA", "MINI RT01"),
]

# ---------------------------------------------------------------------
# Lançamentos reais de julho/2026, extraídos linha a linha das 23 abas de
# equipamento da planilha original (EQUIPAMENTOS_JUL26). Cada valor foi
# conferido contra os totais "DESPESAS RTEC"/"DESPESAS ELDORADO" da aba
# RESULTADO - bate certinho para as 23 máquinas. Usado só pela rota
# /admin/importar-planilha, uma vez.
# ---------------------------------------------------------------------
LANCAMENTOS_PLANILHA_B64 = (
    "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"
)


def carregar_lancamentos_planilha():
    bruto = gzip.decompress(base64.b64decode(LANCAMENTOS_PLANILHA_B64))
    return json.loads(bruto.decode("utf-8"))

# =====================================================================
# Segurança de senha (PBKDF2 puro stdlib - evita conflito de versão do
# passlib/bcrypt observado neste ambiente)
# =====================================================================
ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return hmac.compare_digest(dk.hex(), dk_hex)


# =====================================================================
# Banco de dados
# =====================================================================
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True)
    nome = Column(String(120), nullable=False)
    usuario = Column(String(60), unique=True, nullable=False, index=True)
    senha_hash = Column(String(200), nullable=False)
    empresa = Column(String(40), nullable=False)
    is_admin = Column(Boolean, default=False)
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.datetime.utcnow)
    lancamentos = relationship("Lancamento", back_populates="usuario")


class Equipamento(Base):
    __tablename__ = "equipamentos"
    id = Column(Integer, primary_key=True)
    nome = Column(String(120), nullable=False)
    identificacao = Column(String(60), nullable=True)
    ativo = Column(Boolean, default=True)
    # Qual das duas empresas fatura (cobra do cliente) esse equipamento -
    # usado no Resultado para saber quem "recebeu" o faturamento dele.
    empresa_faturamento = Column(String(40), nullable=True)
    lancamentos = relationship("Lancamento", back_populates="equipamento")
    faturamentos = relationship("Faturamento", back_populates="equipamento")

    @property
    def rotulo(self):
        return f"{self.nome} ({self.identificacao})" if self.identificacao else self.nome


class Lancamento(Base):
    __tablename__ = "lancamentos"
    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    empresa = Column(String(40), nullable=False)
    equipamento_id = Column(Integer, ForeignKey("equipamentos.id"), nullable=False)
    categoria = Column(String(40), nullable=False)
    data_despesa = Column(Date, nullable=False)
    descricao = Column(Text, nullable=False)
    qtd = Column(Float, nullable=False, default=1)
    valor_unitario = Column(Float, nullable=False, default=0)
    valor_total = Column(Float, nullable=False, default=0)
    criado_em = Column(DateTime, default=datetime.datetime.utcnow)
    usuario = relationship("Usuario", back_populates="lancamentos")
    equipamento = relationship("Equipamento", back_populates="lancamentos")


class Faturamento(Base):
    """Receita faturada de um equipamento (o que a planilha chamava de
    VL. FATURADO). Lançado manualmente, um valor por vez, igual a uma
    despesa - mas sem categoria nem empresa (a empresa é a do próprio
    equipamento, ver Equipamento.empresa_faturamento)."""
    __tablename__ = "faturamentos"
    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    equipamento_id = Column(Integer, ForeignKey("equipamentos.id"), nullable=False)
    data_faturamento = Column(Date, nullable=False)
    descricao = Column(Text, nullable=True)
    valor = Column(Float, nullable=False, default=0)
    criado_em = Column(DateTime, default=datetime.datetime.utcnow)
    usuario = relationship("Usuario")
    equipamento = relationship("Equipamento", back_populates="faturamentos")


class DespesaExtra(Base):
    """Despesa que NÃO é de um equipamento específico (diesel, juros de
    financiamento, outros negócios, etc.) - fica de fora da tabela por
    equipamento mas entra no acerto de lucro entre as duas empresas.
    Cada empresa lança a sua, sempre na própria empresa do usuário logado."""
    __tablename__ = "despesas_extras"
    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    empresa = Column(String(40), nullable=False)
    data_despesa = Column(Date, nullable=False)
    descricao = Column(Text, nullable=False)
    valor = Column(Float, nullable=False, default=0)
    criado_em = Column(DateTime, default=datetime.datetime.utcnow)
    usuario = relationship("Usuario")


class Observacao(Base):
    """Anotação de conferência/aviso - valores que aparecem soltos na
    planilha original (fora de qualquer fórmula que soma no Resultado),
    tipo checagens de recebíveis, resultados de outros negócios, etc.
    Fica só como referência: NÃO entra em nenhuma conta do sistema
    (nem no Resumo, nem no acerto do Resultado)."""
    __tablename__ = "observacoes"
    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    empresa = Column(String(40), nullable=True)  # pode ser geral (nenhuma das duas)
    data_referencia = Column(Date, nullable=True)
    descricao = Column(Text, nullable=False)
    valor = Column(Float, nullable=True)
    criado_em = Column(DateTime, default=datetime.datetime.utcnow)
    usuario = relationship("Usuario")


def seed(db: Session):
    if db.query(Equipamento).count() == 0:
        for nome, ident in EQUIPAMENTOS_SEED:
            db.add(Equipamento(
                nome=nome, identificacao=ident,
                empresa_faturamento=EQUIPAMENTOS_FATURAMENTO_EMPRESA.get(nome),
            ))
        db.commit()


def migrar_schema(engine):
    """Ajustes de schema em bancos que já existiam antes dessas mudanças
    (create_all não altera tabelas já criadas, só cria as que faltam)."""
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text(
                "ALTER TABLE equipamentos ADD COLUMN IF NOT EXISTS empresa_faturamento VARCHAR(40)"
            ))
            conn.commit()
        else:
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(equipamentos)"))]
            if "empresa_faturamento" not in cols:
                conn.execute(text("ALTER TABLE equipamentos ADD COLUMN empresa_faturamento VARCHAR(40)"))
                conn.commit()


def aplicar_empresa_faturamento_padrao(db: Session):
    """Preenche empresa_faturamento pelos equipamentos que ainda não têm
    (não sobrescreve o que já foi ajustado manualmente na tela Equipamentos)."""
    mudou = False
    for e in db.query(Equipamento).all():
        alvo = EQUIPAMENTOS_FATURAMENTO_EMPRESA.get(e.nome)
        if alvo and not e.empresa_faturamento:
            e.empresa_faturamento = alvo
            mudou = True
    if mudou:
        db.commit()


# =====================================================================
# Templates (embutidos - sem pasta templates/ separada)
# =====================================================================
CSS = """
:root {
  --azul: #1f4e78; --azul-claro: #eaf1f8; --verde: #1a7a4c;
  --vermelho: #b3261e; --cinza: #666; --borda: #dde3ea; --fundo: #f6f8fa;
}
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; margin: 0; background: var(--fundo); color: #1a1a1a; }
header.topo { background: var(--azul); color: white; padding: 14px 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
header.topo .titulo { font-weight: 700; font-size: 1.05rem; }
nav.abas { display: flex; gap: 4px; flex-wrap: wrap; }
nav.abas a { color: white; text-decoration: none; padding: 8px 12px; border-radius: 6px; font-size: 0.92rem; opacity: 0.88; }
nav.abas a:hover, nav.abas a.ativo { background: rgba(255,255,255,0.18); opacity: 1; }
main { max-width: 1000px; margin: 0 auto; padding: 20px 16px 60px; }
.card { background: white; border: 1px solid var(--borda); border-radius: 10px; padding: 18px; margin-bottom: 18px; }
h1 { font-size: 1.4rem; margin: 0 0 4px; color: var(--azul); }
h2 { font-size: 1.1rem; margin: 0 0 12px; color: var(--azul); }
p.sub { color: var(--cinza); margin: 0 0 20px; font-size: 0.92rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--borda); }
th { color: var(--cinza); font-weight: 600; font-size: 0.82rem; text-transform: uppercase; }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
label { display: block; font-size: 0.85rem; color: var(--cinza); margin: 12px 0 4px; }
input, select, textarea { width: 100%; padding: 9px 10px; border: 1px solid var(--borda); border-radius: 6px; font-size: 0.95rem; font-family: inherit; }
textarea { resize: vertical; min-height: 60px; }
button, .botao { display: inline-block; background: var(--azul); color: white; border: none; padding: 10px 18px; border-radius: 7px; font-size: 0.95rem; cursor: pointer; text-decoration: none; margin-top: 16px; }
button:hover, .botao:hover { opacity: 0.92; }
.botao.perigo { background: white; color: var(--vermelho); border: 1px solid var(--vermelho); padding: 4px 10px; font-size: 0.8rem; margin-top: 0; }
.erro { background: #fdecea; color: var(--vermelho); border: 1px solid #f5c2c0; padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 0.9rem; }
.sucesso { background: #eaf7ef; color: var(--verde); border: 1px solid #bfe6cd; padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 0.9rem; }
.filtros { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
.filtros select { width: auto; min-width: 160px; }
.stat-row { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 20px; }
.stat { background: var(--azul-claro); border-radius: 10px; padding: 14px 18px; flex: 1; min-width: 140px; }
.stat .valor { font-size: 1.5rem; font-weight: 700; color: var(--azul); }
.stat .rotulo { font-size: 0.8rem; color: var(--cinza); text-transform: uppercase; }
a.link-simples { color: var(--azul); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 0.75rem; background: var(--azul-claro); color: var(--azul); }
"""

TEMPLATES = {
    "base.html": """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block titulo %}Equipamentos{% endblock %} - RTEC / Eldorado</title>
  <style>""" + CSS + """</style>
</head>
<body>
  {% if user %}
  <header class="topo">
    <div class="titulo">Equipamentos - Lançamento Remoto</div>
    <nav class="abas">
      <a href="/">Resumo</a>
      <a href="/lancamentos">Lançamentos</a>
      <a href="/lancamentos/novo">+ Despesa</a>
      <a href="/faturamentos/novo">+ Faturamento</a>
      <a href="/despesas-extras/novo">+ Despesa extra</a>
      <a href="/observacoes/novo">+ Observação</a>
      <a href="/resultado">Resultado</a>
      {% if user.is_admin %}<a href="/usuarios">Usuários</a>{% endif %}
      {% if user.is_admin %}<a href="/equipamentos">Equipamentos</a>{% endif %}
      <a href="/logout">Sair ({{ user.nome }})</a>
    </nav>
  </header>
  {% endif %}
  <main>
    {% block conteudo %}{% endblock %}
  </main>
</body>
</html>""",

    "login.html": """{% extends "base.html" %}
{% block titulo %}Entrar{% endblock %}
{% block conteudo %}
<div class="card" style="max-width:380px;margin:40px auto;">
  <h1>Entrar</h1>
  <p class="sub">RTEC Tratores e Eldorado Serviços</p>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="post" action="/login">
    <label>Usuário</label>
    <input type="text" name="usuario" required autofocus autocapitalize="off">
    <label>Senha</label>
    <input type="password" name="senha" required>
    <button type="submit">Entrar</button>
  </form>
  <p style="margin-top:16px;font-size:0.9rem;">Ainda não tem conta? <a class="link-simples" href="/registrar">Criar conta</a></p>
</div>
{% endblock %}""",

    "registrar.html": """{% extends "base.html" %}
{% block titulo %}Criar conta{% endblock %}
{% block conteudo %}
<div class="card" style="max-width:420px;margin:40px auto;">
  <h1>Criar conta</h1>
  <p class="sub">Peça o código de convite da sua empresa para quem administra o sistema.</p>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="post" action="/registrar">
    <label>Nome completo</label>
    <input type="text" name="nome" required>
    <label>Empresa</label>
    <select name="empresa" required>
      {% for e in empresas %}<option value="{{ e }}">{{ e }}</option>{% endfor %}
    </select>
    <label>Código de convite da empresa</label>
    <input type="text" name="codigo_convite" required>
    <label>Usuário (login)</label>
    <input type="text" name="usuario" required autocapitalize="off">
    <label>Senha (mínimo 6 caracteres)</label>
    <input type="password" name="senha" required minlength="6">
    <button type="submit">Criar conta e entrar</button>
  </form>
  <p style="margin-top:16px;font-size:0.9rem;">Já tem conta? <a class="link-simples" href="/login">Entrar</a></p>
</div>
{% endblock %}""",

    "dashboard.html": """{% extends "base.html" %}
{% block titulo %}Resumo{% endblock %}
{% block conteudo %}
<h1>Resumo</h1>
<p class="sub">Totais calculados automaticamente a partir de todos os lançamentos.</p>

<div class="stat-row">
  {% for emp in empresas %}
  <div class="stat">
    <div class="valor">R$ {{ "%.2f"|format(matriz[emp].values()|sum) }}</div>
    <div class="rotulo">{{ emp }}</div>
  </div>
  {% endfor %}
</div>

<div class="card">
  <h2>Por empresa e categoria</h2>
  <table>
    <thead>
      <tr>
        <th>Empresa</th>
        {% for cat in categorias %}<th class="num">{{ cat }}</th>{% endfor %}
        <th class="num">Total</th>
      </tr>
    </thead>
    <tbody>
      {% for emp in empresas %}
      <tr>
        <td>{{ emp }}</td>
        {% for cat in categorias %}<td class="num">R$ {{ "%.2f"|format(matriz[emp][cat]) }}</td>{% endfor %}
        <td class="num"><strong>R$ {{ "%.2f"|format(matriz[emp].values()|sum) }}</strong></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<div class="card">
  <h2>Por equipamento</h2>
  <table>
    <thead>
      <tr><th>Equipamento</th><th class="num">RTEC Tratores</th><th class="num">Eldorado Serviços</th><th class="num">Total</th></tr>
    </thead>
    <tbody>
      {% for e in equipamentos %}
      {% set t = equip_totais[e.id] %}
      {% if (t.values()|sum) > 0 %}
      <tr>
        <td>{{ e.rotulo }}</td>
        <td class="num">R$ {{ "%.2f"|format(t.get("RTEC TRATORES", 0)) }}</td>
        <td class="num">R$ {{ "%.2f"|format(t.get("ELDORADO SERVIÇOS", 0)) }}</td>
        <td class="num"><strong>R$ {{ "%.2f"|format(t.values()|sum) }}</strong></td>
      </tr>
      {% endif %}
      {% endfor %}
    </tbody>
  </table>
  {% if not tem_lancamentos %}
  <p class="sub" style="margin-top:12px;">Nenhum lançamento ainda. <a class="link-simples" href="/lancamentos/novo">Fazer o primeiro lançamento</a>.</p>
  {% endif %}
</div>

<div class="card">
  <h2>Últimos lançamentos</h2>
  {% if ultimos %}
  <table>
    <thead><tr><th>Data</th><th>Empresa</th><th>Equipamento</th><th>Categoria</th><th class="num">Valor</th><th>Lançado por</th></tr></thead>
    <tbody>
      {% for l in ultimos %}
      <tr>
        <td>{{ l.data_despesa.strftime("%d/%m/%Y") }}</td>
        <td>{{ l.empresa }}</td>
        <td>{{ l.equipamento.rotulo }}</td>
        <td>{{ l.categoria }}</td>
        <td class="num">R$ {{ "%.2f"|format(l.valor_total) }}</td>
        <td>{{ l.usuario.nome }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p class="sub">Nenhum lançamento ainda.</p>
  {% endif %}
</div>
{% endblock %}""",

    "faturamento_novo.html": """{% extends "base.html" %}
{% block titulo %}Novo faturamento{% endblock %}
{% block conteudo %}
<div class="card" style="max-width:520px;">
  <h1>Novo faturamento</h1>
  <p class="sub">Valor faturado (cobrado do cliente) de um equipamento · lançado por {{ user.nome }}</p>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="post" action="/faturamentos/novo">
    <label>Equipamento</label>
    <select name="equipamento_id" required>
      {% for e in equipamentos %}
      <option value="{{ e.id }}">{{ e.rotulo }}{% if e.empresa_faturamento %} — faturado pela {{ e.empresa_faturamento }}{% endif %}</option>
      {% endfor %}
    </select>
    <label>Data</label>
    <input type="date" name="data_faturamento" value="{{ hoje }}" required>
    <label>Descrição (opcional)</label>
    <textarea name="descricao" placeholder="Ex.: faturamento de julho/2026"></textarea>
    <label>Valor faturado (R$)</label>
    <input type="number" name="valor" value="0" min="0" step="0.01" required>
    <button type="submit">Lançar faturamento</button>
  </form>
</div>
{% endblock %}""",

    "despesa_extra_novo.html": """{% extends "base.html" %}
{% block titulo %}Nova despesa extra{% endblock %}
{% block conteudo %}
<div class="card" style="max-width:520px;">
  <h1>Nova despesa (não é de equipamento)</h1>
  <p class="sub">Empresa: <span class="badge">{{ user.empresa }}</span> · lançado por {{ user.nome }} · entra no acerto do Resultado, mas não em nenhum equipamento (ex.: diesel, juros de financiamento, outros negócios)</p>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="post" action="/despesas-extras/novo">
    <label>Data</label>
    <input type="date" name="data_despesa" value="{{ hoje }}" required>
    <label>Descrição</label>
    <textarea name="descricao" required placeholder="Ex.: diesel pago em 03/07, juros 2% financiamento escavadeira"></textarea>
    <label>Valor (R$)</label>
    <input type="number" name="valor" value="0" min="0" step="0.01" required>
    <button type="submit">Lançar despesa</button>
  </form>
</div>
{% endblock %}""",

    "observacao_novo.html": """{% extends "base.html" %}
{% block titulo %}Nova observação{% endblock %}
{% block conteudo %}
<div class="card" style="max-width:520px;">
  <h1>Nova observação</h1>
  <p class="sub">Anotação/aviso de conferência (ex.: valor a receber de outro negócio, resultado de outro imóvel, ajuste pendente). <strong>Não entra em nenhuma conta do sistema</strong> — fica só como referência na tela Resultado. Lançado por {{ user.nome }}.</p>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="post" action="/observacoes/novo">
    <label>Empresa (opcional)</label>
    <select name="empresa">
      <option value="">Geral (nenhuma das duas)</option>
      <option value="RTEC TRATORES">RTEC TRATORES</option>
      <option value="ELDORADO SERVIÇOS">ELDORADO SERVIÇOS</option>
    </select>
    <label>Data de referência (opcional)</label>
    <input type="date" name="data_referencia">
    <label>Descrição</label>
    <textarea name="descricao" required placeholder="Ex.: resultado BMS imóvel X, valor a receber de outro negócio"></textarea>
    <label>Valor (opcional, R$)</label>
    <input type="number" name="valor" step="0.01" placeholder="Deixe em branco se não tiver valor">
    <button type="submit">Salvar observação</button>
  </form>
</div>
{% endblock %}""",

    "novo_lancamento.html": """{% extends "base.html" %}
{% block titulo %}Novo lançamento{% endblock %}
{% block conteudo %}
<div class="card" style="max-width:520px;">
  <h1>Novo lançamento</h1>
  <p class="sub">Empresa: <span class="badge">{{ user.empresa }}</span> · lançado por {{ user.nome }}</p>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="post" action="/lancamentos/novo">
    <label>Equipamento</label>
    <select name="equipamento_id" required>
      {% for e in equipamentos %}<option value="{{ e.id }}">{{ e.rotulo }}</option>{% endfor %}
    </select>
    <label>Categoria</label>
    <select name="categoria" required>
      {% for c in categorias %}<option value="{{ c }}">{{ c }}</option>{% endfor %}
    </select>
    <label>Data da despesa</label>
    <input type="date" name="data_despesa" value="{{ hoje }}" required>
    <label>Descrição</label>
    <textarea name="descricao" required placeholder="Ex.: troca de filtro de óleo"></textarea>
    <div class="grid">
      <div>
        <label>Quantidade</label>
        <input type="number" name="qtd" value="1" min="0.01" step="0.01" required>
      </div>
      <div>
        <label>Valor unitário (R$)</label>
        <input type="number" name="valor_unitario" value="0" min="0" step="0.01" required>
      </div>
    </div>
    <button type="submit">Lançar despesa</button>
  </form>
</div>
{% endblock %}""",

    "lancamentos.html": """{% extends "base.html" %}
{% block titulo %}Lançamentos{% endblock %}
{% block conteudo %}
<h1>Lançamentos</h1>
<p class="sub">Todos os lançamentos das duas empresas. <a class="link-simples" href="/lancamentos/exportar.csv">Exportar CSV</a></p>

{% if request.query_params.get("criado") %}<div class="sucesso">Lançamento salvo com sucesso.</div>{% endif %}

<form method="get" action="/lancamentos" class="filtros">
  <select name="empresa" onchange="this.form.submit()">
    <option value="">Todas as empresas</option>
    {% for e in empresas %}<option value="{{ e }}" {% if filtro_empresa==e %}selected{% endif %}>{{ e }}</option>{% endfor %}
  </select>
  <select name="equipamento_id" onchange="this.form.submit()">
    <option value="">Todos os equipamentos</option>
    {% for eq in equipamentos %}<option value="{{ eq.id }}" {% if filtro_equipamento==eq.id %}selected{% endif %}>{{ eq.rotulo }}</option>{% endfor %}
  </select>
  <select name="categoria" onchange="this.form.submit()">
    <option value="">Todas as categorias</option>
    {% for c in categorias %}<option value="{{ c }}" {% if filtro_categoria==c %}selected{% endif %}>{{ c }}</option>{% endfor %}
  </select>
</form>

<div class="card">
  {% if lancamentos %}
  <table>
    <thead>
      <tr><th>Data</th><th>Empresa</th><th>Equipamento</th><th>Categoria</th><th>Descrição</th><th class="num">Qtd</th><th class="num">Vl. Unit.</th><th class="num">Total</th><th>Por</th><th></th></tr>
    </thead>
    <tbody>
      {% for l in lancamentos %}
      <tr>
        <td>{{ l.data_despesa.strftime("%d/%m/%Y") }}</td>
        <td>{{ l.empresa }}</td>
        <td>{{ l.equipamento.rotulo }}</td>
        <td>{{ l.categoria }}</td>
        <td>{{ l.descricao }}</td>
        <td class="num">{{ l.qtd }}</td>
        <td class="num">R$ {{ "%.2f"|format(l.valor_unitario) }}</td>
        <td class="num">R$ {{ "%.2f"|format(l.valor_total) }}</td>
        <td>{{ l.usuario.nome }}</td>
        <td>
          {% if l.usuario_id == user.id or user.is_admin %}
          <form method="post" action="/lancamentos/{{ l.id }}/excluir" onsubmit="return confirm('Excluir este lançamento?');">
            <button type="submit" class="botao perigo">Excluir</button>
          </form>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p class="sub">Nenhum lançamento encontrado.</p>
  {% endif %}
</div>
{% endblock %}""",

    "usuarios.html": """{% extends "base.html" %}
{% block titulo %}Usuários{% endblock %}
{% block conteudo %}
<h1>Usuários</h1>
<p class="sub">Quem pode lançar despesas no sistema. Para adicionar alguém, peça para essa pessoa entrar em /registrar com o código de convite da empresa dela.</p>
<div class="card">
  <table>
    <thead><tr><th>Nome</th><th>Usuário</th><th>Empresa</th><th>Situação</th><th></th></tr></thead>
    <tbody>
      {% for u in usuarios %}
      <tr>
        <td>{{ u.nome }}{% if u.is_admin %} <span class="badge">admin</span>{% endif %}</td>
        <td>{{ u.usuario }}</td>
        <td>{{ u.empresa }}</td>
        <td>{{ "Ativo" if u.ativo else "Desativado" }}</td>
        <td>
          {% if u.id != user.id %}
          <form method="post" action="/usuarios/{{ u.id }}/alternar">
            <button type="submit" class="botao secundario" style="margin-top:0;padding:4px 10px;font-size:0.8rem;">
              {{ "Desativar" if u.ativo else "Reativar" }}
            </button>
          </form>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}""",

    "resultado.html": """{% extends "base.html" %}
{% block titulo %}Resultado{% endblock %}
{% block conteudo %}
<h1>Resultado</h1>
<p class="sub">Faturamento menos despesas de cada equipamento e despesas extras, e o acerto do lucro entre as duas empresas (50/50).</p>

{% if request.query_params.get("criado") %}<div class="sucesso">Lançamento salvo com sucesso.</div>{% endif %}

<div class="stat-row">
  <div class="stat"><div class="valor">R$ {{ "%.2f"|format(total_faturado) }}</div><div class="rotulo">Faturamento total</div></div>
  <div class="stat"><div class="valor">R$ {{ "%.2f"|format(total_despesas + total_extras_rtec + total_extras_eldorado) }}</div><div class="rotulo">Despesas totais (equipamentos + extras)</div></div>
  <div class="stat"><div class="valor">R$ {{ "%.2f"|format(lucro_total) }}</div><div class="rotulo">Lucro total</div></div>
</div>

<div class="card">
  <h2>Por equipamento</h2>
  <table>
    <thead>
      <tr><th>Equipamento</th><th>Faturado por</th><th class="num">Faturamento</th><th class="num">Despesas RTEC</th><th class="num">Despesas Eldorado</th><th class="num">Total despesas</th><th class="num">Resultado</th></tr>
    </thead>
    <tbody>
      {% for l in linhas %}
      <tr>
        <td>{{ l.equipamento.rotulo }}</td>
        <td>{{ l.equipamento.empresa_faturamento or "—" }}</td>
        <td class="num">R$ {{ "%.2f"|format(l.faturamento) }}</td>
        <td class="num">R$ {{ "%.2f"|format(l.despesas_rtec) }}</td>
        <td class="num">R$ {{ "%.2f"|format(l.despesas_eldorado) }}</td>
        <td class="num">R$ {{ "%.2f"|format(l.total_despesas) }}</td>
        <td class="num"><strong>R$ {{ "%.2f"|format(l.resultado) }}</strong></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% if not linhas %}<p class="sub" style="margin-top:12px;">Nenhum equipamento cadastrado.</p>{% endif %}
</div>

<div class="card">
  <h2>Despesas extras (não são de equipamento)</h2>
  <p class="sub">Diesel, juros de financiamento, outros negócios etc. — cada empresa lança a sua em <a class="link-simples" href="/despesas-extras/novo">+ Despesa extra</a>. Entram no acerto abaixo.</p>
  <div class="grid">
    <div>
      <h2 style="font-size:0.95rem;">RTEC Tratores <span class="badge">R$ {{ "%.2f"|format(total_extras_rtec) }}</span></h2>
      <table>
        <thead><tr><th>Data</th><th>Descrição</th><th class="num">Valor</th><th></th></tr></thead>
        <tbody>
          {% for x in extras_rtec %}
          <tr>
            <td>{{ x.data_despesa.strftime("%d/%m/%Y") }}</td>
            <td>{{ x.descricao }}</td>
            <td class="num">R$ {{ "%.2f"|format(x.valor) }}</td>
            <td>
              {% if x.usuario_id == user.id or user.is_admin %}
              <form method="post" action="/despesas-extras/{{ x.id }}/excluir" onsubmit="return confirm('Excluir?');">
                <button type="submit" class="botao perigo">Excluir</button>
              </form>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
          {% if not extras_rtec %}<tr><td colspan="4" class="sub">Nenhuma.</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
    <div>
      <h2 style="font-size:0.95rem;">Eldorado Serviços <span class="badge">R$ {{ "%.2f"|format(total_extras_eldorado) }}</span></h2>
      <table>
        <thead><tr><th>Data</th><th>Descrição</th><th class="num">Valor</th><th></th></tr></thead>
        <tbody>
          {% for x in extras_eldorado %}
          <tr>
            <td>{{ x.data_despesa.strftime("%d/%m/%Y") }}</td>
            <td>{{ x.descricao }}</td>
            <td class="num">R$ {{ "%.2f"|format(x.valor) }}</td>
            <td>
              {% if x.usuario_id == user.id or user.is_admin %}
              <form method="post" action="/despesas-extras/{{ x.id }}/excluir" onsubmit="return confirm('Excluir?');">
                <button type="submit" class="botao perigo">Excluir</button>
              </form>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
          {% if not extras_eldorado %}<tr><td colspan="4" class="sub">Nenhuma.</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
  </div>
</div>

<div class="card">
  <h2>Observações <span class="sub" style="font-weight:normal;">(conferência/aviso — não entra em nenhuma conta)</span></h2>
  <p class="sub">Anotações soltas tipo valores a receber de outros negócios, resultado de outros imóveis, ajustes pendentes etc. Lance em <a class="link-simples" href="/observacoes/novo">+ Observação</a>. Não afeta o Resumo nem o acerto abaixo.</p>
  <table>
    <thead><tr><th>Data</th><th>Empresa</th><th>Descrição</th><th class="num">Valor</th><th></th></tr></thead>
    <tbody>
      {% for o in observacoes %}
      <tr>
        <td>{{ o.data_referencia.strftime("%d/%m/%Y") if o.data_referencia else "—" }}</td>
        <td>{{ o.empresa or "Geral" }}</td>
        <td>{{ o.descricao }}</td>
        <td class="num">{{ "R$ %.2f"|format(o.valor) if o.valor is not none else "—" }}</td>
        <td>
          {% if o.usuario_id == user.id or user.is_admin %}
          <form method="post" action="/observacoes/{{ o.id }}/excluir" onsubmit="return confirm('Excluir?');">
            <button type="submit" class="botao perigo">Excluir</button>
          </form>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
      {% if not observacoes %}<tr><td colspan="5" class="sub">Nenhuma.</td></tr>{% endif %}
    </tbody>
  </table>
</div>

<div class="card">
  <h2>Acerto entre as empresas</h2>
  <p class="sub">O lucro total (faturamento − despesas de equipamento − despesas extras) é dividido meio a meio. Cada empresa desconta o que ela mesma pagou (equipamentos + extras) e o que já recebeu diretamente (faturamento dos equipamentos que fatura). A diferença é o que falta acertar entre as duas.</p>
  <table>
    <thead><tr><th></th><th class="num">RTEC Tratores</th><th class="num">Eldorado Serviços</th></tr></thead>
    <tbody>
      <tr><td>Despesas de equipamentos pagas por ela</td><td class="num">R$ {{ "%.2f"|format(total_despesas_rtec) }}</td><td class="num">R$ {{ "%.2f"|format(total_despesas_eldorado) }}</td></tr>
      <tr><td>+ Despesas extras pagas por ela</td><td class="num">R$ {{ "%.2f"|format(total_extras_rtec) }}</td><td class="num">R$ {{ "%.2f"|format(total_extras_eldorado) }}</td></tr>
      <tr><td>+ metade do lucro total</td><td class="num">R$ {{ "%.2f"|format(resultado_rtec) }}</td><td class="num">R$ {{ "%.2f"|format(resultado_eldorado) }}</td></tr>
      <tr><td><strong>= Total a receber</strong></td><td class="num"><strong>R$ {{ "%.2f"|format(a_receber_rtec) }}</strong></td><td class="num"><strong>R$ {{ "%.2f"|format(a_receber_eldorado) }}</strong></td></tr>
      <tr><td>Já recebido direto (equip. que fatura)</td><td class="num">R$ {{ "%.2f"|format(recebido_rtec) }}</td><td class="num">R$ {{ "%.2f"|format(recebido_eldorado) }}</td></tr>
      <tr><td><strong>= Diferença do acerto</strong></td><td class="num"><strong>R$ {{ "%.2f"|format(diferenca_rtec) }}</strong></td><td class="num"><strong>R$ {{ "%.2f"|format(diferenca_eldorado) }}</strong></td></tr>
    </tbody>
  </table>
  <p class="sub" style="margin-top:12px;">Diferença positiva = essa empresa ainda tem a receber da outra.</p>
</div>
{% endblock %}""",

    "equipamentos.html": """{% extends "base.html" %}
{% block titulo %}Equipamentos{% endblock %}
{% block conteudo %}
<h1>Equipamentos</h1>
<p class="sub">Qual empresa fatura (cobra do cliente) cada equipamento - usado na tela Resultado.</p>
{% if sucesso %}<div class="sucesso">Atualizado.</div>{% endif %}
<div class="card">
  <table>
    <thead><tr><th>Equipamento</th><th>Identificação</th><th>Faturado por</th><th></th></tr></thead>
    <tbody>
      {% for e in equipamentos %}
      <tr>
        <td>{{ e.nome }}</td>
        <td>{{ e.identificacao or "" }}</td>
        <td>{{ e.empresa_faturamento or "não definido" }}</td>
        <td>
          <form method="post" action="/equipamentos/{{ e.id }}/empresa-faturamento" style="display:flex;gap:6px;align-items:center;">
            <select name="empresa_faturamento" style="width:auto;min-width:170px;">
              {% for emp in empresas %}<option value="{{ emp }}" {% if e.empresa_faturamento==emp %}selected{% endif %}>{{ emp }}</option>{% endfor %}
            </select>
            <button type="submit" style="margin-top:0;padding:6px 12px;font-size:0.85rem;">Salvar</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}""",
}

jinja_env = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx["request"] = request
    ctx.setdefault("user", None)
    template = jinja_env.get_template(name)
    return HTMLResponse(template.render(**ctx))


# =====================================================================
# App
# =====================================================================
app = FastAPI(title="Equipamentos - Lançamento Remoto")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    migrar_schema(engine)
    db = SessionLocal()
    try:
        seed(db)
        aplicar_empresa_faturamento_padrao(db)
    finally:
        db.close()


def current_user(request: Request, db: Session) -> Optional[Usuario]:
    uid = request.session.get("user_id")
    if not uid:
        return None
    return db.query(Usuario).filter(Usuario.id == uid, Usuario.ativo == True).first()  # noqa: E712


@app.exception_handler(HTTPException)
async def redirect_on_401(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_303_SEE_OTHER and "Location" in (exc.headers or {}):
        return RedirectResponse(url=exc.headers["Location"], status_code=303)
    return await http_exception_handler(request, exc)


# ---------------------------------------------------------------- auth
@app.get("/registrar", response_class=HTMLResponse)
def registrar_form(request: Request):
    return render(request, "registrar.html", empresas=EMPRESAS, erro=None)


@app.post("/registrar")
def registrar(
    request: Request,
    nome: str = Form(...),
    empresa: str = Form(...),
    usuario: str = Form(...),
    senha: str = Form(...),
    codigo_convite: str = Form(...),
    db: Session = Depends(get_db),
):
    erro = None
    if empresa not in EMPRESAS:
        erro = "Empresa inválida."
    elif INVITE_CODES.get(empresa) != codigo_convite.strip():
        erro = "Código de convite incorreto para essa empresa."
    elif len(senha) < 6:
        erro = "A senha precisa ter pelo menos 6 caracteres."
    elif db.query(Usuario).filter(Usuario.usuario == usuario.strip().lower()).first():
        erro = "Esse nome de usuário já existe. Escolha outro."

    if erro:
        return render(request, "registrar.html", empresas=EMPRESAS, erro=erro)

    is_first_user = db.query(Usuario).count() == 0
    novo = Usuario(
        nome=nome.strip(),
        usuario=usuario.strip().lower(),
        senha_hash=hash_password(senha),
        empresa=empresa,
        is_admin=is_first_user,
    )
    db.add(novo)
    db.commit()
    request.session["user_id"] = novo.id
    return RedirectResponse(url="/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html", erro=None)


@app.post("/login")
def login(request: Request, usuario: str = Form(...), senha: str = Form(...), db: Session = Depends(get_db)):
    u = db.query(Usuario).filter(Usuario.usuario == usuario.strip().lower(), Usuario.ativo == True).first()  # noqa: E712
    if not u or not verify_password(senha, u.senha_hash):
        return render(request, "login.html", erro="Usuário ou senha incorretos.")
    request.session["user_id"] = u.id
    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    por_empresa_categoria = (
        db.query(Lancamento.empresa, Lancamento.categoria, func.sum(Lancamento.valor_total))
        .group_by(Lancamento.empresa, Lancamento.categoria)
        .all()
    )
    matriz = {emp: {cat: 0.0 for cat in CATEGORIAS} for emp in EMPRESAS}
    for emp, cat, total in por_empresa_categoria:
        if emp in matriz and cat in matriz[emp]:
            matriz[emp][cat] = total or 0.0

    por_equipamento = (
        db.query(Equipamento, Lancamento.empresa, func.sum(Lancamento.valor_total))
        .join(Lancamento, Lancamento.equipamento_id == Equipamento.id)
        .group_by(Equipamento.id, Lancamento.empresa)
        .all()
    )
    equipamentos = db.query(Equipamento).filter(Equipamento.ativo == True).order_by(Equipamento.nome).all()  # noqa: E712
    equip_totais = {e.id: {emp: 0.0 for emp in EMPRESAS} for e in equipamentos}
    for equip, emp, total in por_equipamento:
        if equip.id in equip_totais and emp in equip_totais[equip.id]:
            equip_totais[equip.id][emp] = total or 0.0

    ultimos = db.query(Lancamento).order_by(Lancamento.criado_em.desc()).limit(10).all()
    tem_lancamentos = len(por_empresa_categoria) > 0

    return render(
        request, "dashboard.html", user=user, empresas=EMPRESAS, categorias=CATEGORIAS,
        matriz=matriz, equipamentos=equipamentos, equip_totais=equip_totais, ultimos=ultimos,
        tem_lancamentos=tem_lancamentos,
    )


# ---------------------------------------------------------------- lançamentos
@app.get("/lancamentos", response_class=HTMLResponse)
def listar_lancamentos(
    request: Request,
    empresa: Optional[str] = None,
    equipamento_id: Optional[int] = None,
    categoria: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    q = db.query(Lancamento).order_by(Lancamento.data_despesa.desc(), Lancamento.criado_em.desc())
    if empresa:
        q = q.filter(Lancamento.empresa == empresa)
    if equipamento_id:
        q = q.filter(Lancamento.equipamento_id == equipamento_id)
    if categoria:
        q = q.filter(Lancamento.categoria == categoria)
    lancamentos = q.limit(500).all()

    equipamentos = db.query(Equipamento).filter(Equipamento.ativo == True).order_by(Equipamento.nome).all()  # noqa: E712
    return render(
        request, "lancamentos.html", user=user, lancamentos=lancamentos, empresas=EMPRESAS,
        categorias=CATEGORIAS, equipamentos=equipamentos,
        filtro_empresa=empresa, filtro_equipamento=equipamento_id, filtro_categoria=categoria,
    )


@app.get("/lancamentos/novo", response_class=HTMLResponse)
def novo_lancamento_form(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    equipamentos = db.query(Equipamento).filter(Equipamento.ativo == True).order_by(Equipamento.nome).all()  # noqa: E712
    return render(
        request, "novo_lancamento.html", user=user, equipamentos=equipamentos,
        categorias=CATEGORIAS, hoje=datetime.date.today().isoformat(), erro=None,
    )


@app.post("/lancamentos/novo")
def criar_lancamento(
    request: Request,
    equipamento_id: int = Form(...),
    categoria: str = Form(...),
    data_despesa: str = Form(...),
    descricao: str = Form(...),
    qtd: float = Form(...),
    valor_unitario: float = Form(...),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    equipamentos = db.query(Equipamento).filter(Equipamento.ativo == True).order_by(Equipamento.nome).all()  # noqa: E712
    erro = None
    if categoria not in CATEGORIAS:
        erro = "Categoria inválida."
    equip = db.query(Equipamento).filter(Equipamento.id == equipamento_id).first()
    if not equip:
        erro = "Equipamento inválido."
    if qtd <= 0:
        erro = "Quantidade precisa ser maior que zero."
    if valor_unitario < 0:
        erro = "Valor unitário não pode ser negativo."
    try:
        data_obj = datetime.date.fromisoformat(data_despesa)
    except ValueError:
        erro = "Data inválida."
        data_obj = None

    if erro:
        return render(
            request, "novo_lancamento.html", user=user, equipamentos=equipamentos,
            categorias=CATEGORIAS, hoje=datetime.date.today().isoformat(), erro=erro,
        )

    lanc = Lancamento(
        usuario_id=user.id,
        empresa=user.empresa,
        equipamento_id=equip.id,
        categoria=categoria,
        data_despesa=data_obj,
        descricao=descricao.strip(),
        qtd=qtd,
        valor_unitario=valor_unitario,
        valor_total=round(qtd * valor_unitario, 2),
    )
    db.add(lanc)
    db.commit()
    return RedirectResponse(url="/lancamentos?criado=1", status_code=303)


@app.post("/lancamentos/{lancamento_id}/excluir")
def excluir_lancamento(lancamento_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    lanc = db.query(Lancamento).filter(Lancamento.id == lancamento_id).first()
    if lanc and (lanc.usuario_id == user.id or user.is_admin):
        db.delete(lanc)
        db.commit()
    return RedirectResponse(url="/lancamentos", status_code=303)


# ---------------------------------------------------------------- faturamento
@app.get("/faturamentos/novo", response_class=HTMLResponse)
def novo_faturamento_form(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    equipamentos = db.query(Equipamento).filter(Equipamento.ativo == True).order_by(Equipamento.nome).all()  # noqa: E712
    return render(
        request, "faturamento_novo.html", user=user, equipamentos=equipamentos,
        hoje=datetime.date.today().isoformat(), erro=None,
    )


@app.post("/faturamentos/novo")
def criar_faturamento(
    request: Request,
    equipamento_id: int = Form(...),
    data_faturamento: str = Form(...),
    descricao: str = Form(""),
    valor: float = Form(...),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    equipamentos = db.query(Equipamento).filter(Equipamento.ativo == True).order_by(Equipamento.nome).all()  # noqa: E712
    erro = None
    equip = db.query(Equipamento).filter(Equipamento.id == equipamento_id).first()
    if not equip:
        erro = "Equipamento inválido."
    if valor < 0:
        erro = "Valor não pode ser negativo."
    try:
        data_obj = datetime.date.fromisoformat(data_faturamento)
    except ValueError:
        erro = "Data inválida."
        data_obj = None

    if erro:
        return render(
            request, "faturamento_novo.html", user=user, equipamentos=equipamentos,
            hoje=datetime.date.today().isoformat(), erro=erro,
        )

    fat = Faturamento(
        usuario_id=user.id,
        equipamento_id=equip.id,
        data_faturamento=data_obj,
        descricao=descricao.strip() or None,
        valor=valor,
    )
    db.add(fat)
    db.commit()
    return RedirectResponse(url="/resultado?criado=1", status_code=303)


# ---------------------------------------------------------------- despesas extras (não são de equipamento)
@app.get("/despesas-extras/novo", response_class=HTMLResponse)
def novo_despesa_extra_form(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return render(
        request, "despesa_extra_novo.html", user=user,
        hoje=datetime.date.today().isoformat(), erro=None,
    )


@app.post("/despesas-extras/novo")
def criar_despesa_extra(
    request: Request,
    data_despesa: str = Form(...),
    descricao: str = Form(...),
    valor: float = Form(...),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    erro = None
    if valor < 0:
        erro = "Valor não pode ser negativo."
    if not descricao.strip():
        erro = "Descrição é obrigatória."
    try:
        data_obj = datetime.date.fromisoformat(data_despesa)
    except ValueError:
        erro = "Data inválida."
        data_obj = None

    if erro:
        return render(
            request, "despesa_extra_novo.html", user=user,
            hoje=datetime.date.today().isoformat(), erro=erro,
        )

    de = DespesaExtra(
        usuario_id=user.id,
        empresa=user.empresa,
        data_despesa=data_obj,
        descricao=descricao.strip(),
        valor=valor,
    )
    db.add(de)
    db.commit()
    return RedirectResponse(url="/resultado?criado=1", status_code=303)


@app.post("/despesas-extras/{despesa_extra_id}/excluir")
def excluir_despesa_extra(despesa_extra_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    de = db.query(DespesaExtra).filter(DespesaExtra.id == despesa_extra_id).first()
    if de and (de.usuario_id == user.id or user.is_admin):
        db.delete(de)
        db.commit()
    return RedirectResponse(url="/resultado", status_code=303)


# ---------------------------------------------------------------- observações (não entram em nenhuma conta)
@app.get("/observacoes/novo", response_class=HTMLResponse)
def nova_observacao_form(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return render(
        request, "observacao_novo.html", user=user,
        hoje=datetime.date.today().isoformat(), erro=None,
    )


@app.post("/observacoes/novo")
def criar_observacao(
    request: Request,
    empresa: str = Form(""),
    data_referencia: str = Form(""),
    descricao: str = Form(...),
    valor: str = Form(""),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    erro = None
    if not descricao.strip():
        erro = "Descrição é obrigatória."
    data_obj = None
    if data_referencia:
        try:
            data_obj = datetime.date.fromisoformat(data_referencia)
        except ValueError:
            erro = "Data inválida."
    valor_obj = None
    if valor.strip():
        try:
            valor_obj = float(valor.replace(",", "."))
        except ValueError:
            erro = "Valor inválido."

    if erro:
        return render(
            request, "observacao_novo.html", user=user,
            hoje=datetime.date.today().isoformat(), erro=erro,
        )

    obs = Observacao(
        usuario_id=user.id,
        empresa=empresa if empresa in EMPRESAS else None,
        data_referencia=data_obj,
        descricao=descricao.strip(),
        valor=valor_obj,
    )
    db.add(obs)
    db.commit()
    return RedirectResponse(url="/resultado?criado=1", status_code=303)


@app.post("/observacoes/{observacao_id}/excluir")
def excluir_observacao(observacao_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    obs = db.query(Observacao).filter(Observacao.id == observacao_id).first()
    if obs and (obs.usuario_id == user.id or user.is_admin):
        db.delete(obs)
        db.commit()
    return RedirectResponse(url="/resultado", status_code=303)


# ---------------------------------------------------------------- resultado
@app.get("/resultado", response_class=HTMLResponse)
def resultado(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    equipamentos = db.query(Equipamento).filter(Equipamento.ativo == True).order_by(Equipamento.nome).all()  # noqa: E712

    faturamento_por_equip = dict(
        db.query(Faturamento.equipamento_id, func.sum(Faturamento.valor))
        .group_by(Faturamento.equipamento_id).all()
    )
    despesas_por_equip_empresa = (
        db.query(Lancamento.equipamento_id, Lancamento.empresa, func.sum(Lancamento.valor_total))
        .group_by(Lancamento.equipamento_id, Lancamento.empresa).all()
    )
    despesas_rtec_por_equip = {}
    despesas_eldorado_por_equip = {}
    for equip_id, emp, total in despesas_por_equip_empresa:
        if emp == "RTEC TRATORES":
            despesas_rtec_por_equip[equip_id] = total or 0.0
        elif emp == "ELDORADO SERVIÇOS":
            despesas_eldorado_por_equip[equip_id] = total or 0.0

    linhas = []
    for e in equipamentos:
        faturamento = faturamento_por_equip.get(e.id, 0.0) or 0.0
        desp_rtec = despesas_rtec_por_equip.get(e.id, 0.0) or 0.0
        desp_eldo = despesas_eldorado_por_equip.get(e.id, 0.0) or 0.0
        total_despesas = desp_rtec + desp_eldo
        linhas.append({
            "equipamento": e, "faturamento": faturamento,
            "despesas_rtec": desp_rtec, "despesas_eldorado": desp_eldo,
            "total_despesas": total_despesas, "resultado": faturamento - total_despesas,
        })

    total_faturado = sum(l["faturamento"] for l in linhas)
    total_despesas_rtec = sum(l["despesas_rtec"] for l in linhas)
    total_despesas_eldorado = sum(l["despesas_eldorado"] for l in linhas)
    total_despesas = total_despesas_rtec + total_despesas_eldorado

    # despesas que não são de nenhum equipamento (diesel, juros, outros
    # negócios etc.) - cada empresa lança a sua, mas entram no mesmo acerto
    extras = db.query(DespesaExtra).order_by(DespesaExtra.data_despesa.desc(), DespesaExtra.criado_em.desc()).all()
    extras_rtec = [x for x in extras if x.empresa == "RTEC TRATORES"]
    extras_eldorado = [x for x in extras if x.empresa == "ELDORADO SERVIÇOS"]
    total_extras_rtec = sum(x.valor for x in extras_rtec)
    total_extras_eldorado = sum(x.valor for x in extras_eldorado)
    total_extras = total_extras_rtec + total_extras_eldorado

    lucro_total = total_faturado - total_despesas - total_extras

    resultado_rtec = lucro_total / 2
    resultado_eldorado = lucro_total / 2
    a_receber_rtec = total_despesas_rtec + total_extras_rtec + resultado_rtec
    a_receber_eldorado = total_despesas_eldorado + total_extras_eldorado + resultado_eldorado

    recebido_rtec = sum(l["faturamento"] for l in linhas if l["equipamento"].empresa_faturamento == "RTEC TRATORES")
    recebido_eldorado = sum(l["faturamento"] for l in linhas if l["equipamento"].empresa_faturamento == "ELDORADO SERVIÇOS")

    diferenca_rtec = a_receber_rtec - recebido_rtec
    diferenca_eldorado = a_receber_eldorado - recebido_eldorado

    # observações: só referência/conferência, não entram em nenhuma conta acima
    observacoes = db.query(Observacao).order_by(Observacao.criado_em.desc()).all()

    return render(
        request, "resultado.html", user=user, linhas=linhas,
        total_faturado=total_faturado, total_despesas=total_despesas,
        total_despesas_rtec=total_despesas_rtec, total_despesas_eldorado=total_despesas_eldorado,
        extras_rtec=extras_rtec, extras_eldorado=extras_eldorado,
        total_extras_rtec=total_extras_rtec, total_extras_eldorado=total_extras_eldorado,
        lucro_total=lucro_total, resultado_rtec=resultado_rtec, resultado_eldorado=resultado_eldorado,
        a_receber_rtec=a_receber_rtec, a_receber_eldorado=a_receber_eldorado,
        recebido_rtec=recebido_rtec, recebido_eldorado=recebido_eldorado,
        diferenca_rtec=diferenca_rtec, diferenca_eldorado=diferenca_eldorado,
        observacoes=observacoes,
    )


@app.get("/lancamentos/exportar.csv")
def exportar_csv(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    lancamentos = db.query(Lancamento).order_by(Lancamento.data_despesa.desc()).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Data", "Empresa", "Equipamento", "Categoria", "Descrição", "Qtd", "Valor Unitário", "Valor Total", "Lançado por"])
    for l in lancamentos:
        w.writerow([
            l.data_despesa.isoformat(), l.empresa, l.equipamento.rotulo, l.categoria,
            l.descricao, l.qtd, l.valor_unitario, l.valor_total, l.usuario.nome,
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=lancamentos.csv"},
    )


# ---------------------------------------------------------------- admin: usuários
@app.get("/usuarios", response_class=HTMLResponse)
def listar_usuarios(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Só administradores podem ver essa página.")
    usuarios = db.query(Usuario).order_by(Usuario.empresa, Usuario.nome).all()
    return render(request, "usuarios.html", user=user, usuarios=usuarios)


@app.post("/usuarios/{usuario_id}/alternar")
def alternar_usuario(usuario_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Só administradores podem fazer isso.")
    alvo = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if alvo and alvo.id != user.id:
        alvo.ativo = not alvo.ativo
        db.commit()
    return RedirectResponse(url="/usuarios", status_code=303)


# ---------------------------------------------------------------- admin: equipamentos
@app.get("/equipamentos", response_class=HTMLResponse)
def listar_equipamentos(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Só administradores podem ver essa página.")
    equipamentos = db.query(Equipamento).order_by(Equipamento.nome).all()
    return render(request, "equipamentos.html", user=user, equipamentos=equipamentos, empresas=EMPRESAS, sucesso=False)


@app.post("/equipamentos/{equipamento_id}/empresa-faturamento")
def alterar_empresa_faturamento(
    equipamento_id: int, request: Request,
    empresa_faturamento: str = Form(...), db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Só administradores podem fazer isso.")
    equip = db.query(Equipamento).filter(Equipamento.id == equipamento_id).first()
    if equip and empresa_faturamento in EMPRESAS:
        equip.empresa_faturamento = empresa_faturamento
        db.commit()
    return RedirectResponse(url="/equipamentos", status_code=303)


# ---------------------------------------------------------------- admin: importação da planilha
@app.get("/admin/importar-planilha")
def importar_planilha(token: str, db: Session = Depends(get_db)):
    if not IMPORT_TOKEN or token != IMPORT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido.")

    dados = carregar_lancamentos_planilha()
    equipamentos_por_nome = {e.nome: e for e in db.query(Equipamento).all()}

    usuarios_importacao = {}
    for empresa in EMPRESAS:
        login = IMPORT_USUARIOS[empresa]
        u = db.query(Usuario).filter(Usuario.usuario == login).first()
        if not u:
            u = Usuario(
                nome="Importação (planilha)",
                usuario=login,
                senha_hash=hash_password(os.urandom(24).hex()),
                empresa=empresa,
                is_admin=False,
                ativo=False,  # conta de sistema, não deve logar
            )
            db.add(u)
            db.flush()
        usuarios_importacao[empresa] = u

    # idempotente: refazer a importação apaga o que foi importado antes e
    # insere de novo, sem duplicar. Não mexe em lançamentos feitos por pessoas.
    ids_importacao = [u.id for u in usuarios_importacao.values()]
    db.query(Lancamento).filter(Lancamento.usuario_id.in_(ids_importacao)).delete(synchronize_session=False)
    db.query(Faturamento).filter(Faturamento.usuario_id.in_(ids_importacao)).delete(synchronize_session=False)
    db.query(Observacao).filter(Observacao.usuario_id.in_(ids_importacao)).delete(synchronize_session=False)
    db.commit()

    inseridos = 0
    ignorados = []
    totais_empresa = {emp: 0.0 for emp in EMPRESAS}
    for item in dados:
        equip = equipamentos_por_nome.get(item["equip_nome"])
        if not equip or item["empresa"] not in usuarios_importacao:
            ignorados.append(item.get("descricao", "?"))
            continue
        lanc = Lancamento(
            usuario_id=usuarios_importacao[item["empresa"]].id,
            empresa=item["empresa"],
            equipamento_id=equip.id,
            categoria=item["categoria"],
            data_despesa=datetime.date.fromisoformat(item["data"]),
            descricao=item["descricao"][:1900],
            qtd=item["qtd"],
            valor_unitario=item["valor_unitario"],
            valor_total=item["valor_total"],
        )
        db.add(lanc)
        inseridos += 1
        totais_empresa[item["empresa"]] += item["valor_total"]
    db.commit()

    faturamentos_inseridos = 0
    total_faturado = 0.0
    for nome, valor in FATURAMENTO_PLANILHA_JUL26.items():
        equip = equipamentos_por_nome.get(nome)
        if not equip:
            continue
        empresa_alvo = equip.empresa_faturamento or EQUIPAMENTOS_FATURAMENTO_EMPRESA.get(nome) or EMPRESAS[0]
        fat = Faturamento(
            usuario_id=usuarios_importacao[empresa_alvo].id,
            equipamento_id=equip.id,
            data_faturamento=datetime.date(2026, 7, 31),
            descricao="Faturamento de julho/2026 (planilha original)",
            valor=valor,
        )
        db.add(fat)
        faturamentos_inseridos += 1
        total_faturado += valor
    db.commit()

    # itens de conferência/aviso da planilha (fora de qualquer fórmula que
    # soma no Resultado) - entram só como Observação, não afetam nenhuma conta
    observacoes_inseridas = 0
    usuario_import_padrao = usuarios_importacao[EMPRESAS[0]]
    for item in OBSERVACOES_PLANILHA_JUL26:
        empresa_item = item["empresa"]
        usuario_obs = usuarios_importacao.get(empresa_item, usuario_import_padrao)
        obs = Observacao(
            usuario_id=usuario_obs.id,
            empresa=empresa_item,
            data_referencia=datetime.date(2026, 7, 31),
            descricao=item["descricao"] + " (importado da planilha - conferência)",
            valor=item["valor"],
        )
        db.add(obs)
        observacoes_inseridas += 1
    db.commit()

    return {
        "ok": True,
        "lancamentos_inseridos": inseridos,
        "ignorados": len(ignorados),
        "totais_por_empresa": {k: round(v, 2) for k, v in totais_empresa.items()},
        "faturamentos_inseridos": faturamentos_inseridos,
        "total_faturado": round(total_faturado, 2),
        "observacoes_inseridas": observacoes_inseridas,
    }
