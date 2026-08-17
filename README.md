# Equipamentos - Lançamento Remoto

Sistema web para RTEC Tratores e Eldorado Serviços lançarem despesas de
equipamentos de qualquer lugar, com login individual por pessoa.

Todo o sistema está em um único arquivo (`main.py`) de propósito - sem
subpastas - para o upload no GitHub nunca correr o risco de "achatar" uma
estrutura de diretórios sem querer.

## O que tem

- Login por pessoa (nome, usuário e senha), vinculado a uma das duas empresas.
- Cadastro de conta protegido por um "código de convite" por empresa.
- Lançamento de despesas por equipamento, categoria (Mão de Obra, Manutenção,
  Combustível, Outras Despesas), data, descrição, quantidade e valor
  unitário - o valor total é calculado sozinho.
- Todo mundo vê os lançamentos das duas empresas.
- Painel com totais por empresa, por categoria e por equipamento.
- Exportação de tudo em CSV.
- Os 23 equipamentos já vêm cadastrados, com os mesmos nomes da planilha
  original (RESULTADO).

## Rodando na sua máquina

```bash
pip install -r requirements.txt
python3 -m uvicorn main:app --reload
```

Abra http://127.0.0.1:8000. Na primeira vez, "Criar conta": a primeira
pessoa vira administradora automaticamente.

Códigos de convite padrão (para teste): `RTEC2026` e `ELDORADO2026`.
**Troque antes de publicar de verdade.**

## Publicando no Render

1. Suba estes arquivos (todos soltos, sem pasta) para um repositório no
   GitHub.
2. Crie uma conta gratuita no [Render](https://render.com) e conecte o
   repositório - o `render.yaml` já configura tudo automaticamente.
3. Defina as variáveis de ambiente (veja abaixo), pelo menos os códigos de
   convite.
4. O Render te dá um link público - esse é o endereço que as duas empresas
   vão usar para lançar despesas.

## Variáveis de ambiente

| Variável | Para quê | Padrão |
|---|---|---|
| `SECRET_KEY` | Assina o cookie de login. | valor de teste, inseguro |
| `INVITE_CODE_RTEC` | Código para a equipe da RTEC criar conta. | `RTEC2026` |
| `INVITE_CODE_ELDORADO` | Código para a equipe da Eldorado criar conta. | `ELDORADO2026` |
| `DATABASE_URL` | Onde os dados ficam guardados. Sem isso, usa SQLite local. | SQLite local |

## Banco de dados para valer

Sem `DATABASE_URL`, os dados ficam num arquivo SQLite local - funciona bem
para testar, mas o disco gratuito do Render não é garantido entre deploys.
Para lançamentos que não podem se perder, crie um Postgres gratuito (Neon,
Supabase, ou o do próprio Render) e cole a connection string em
`DATABASE_URL`. Não precisa mudar nada no código.

## Uso no dia a dia

1. Cada pessoa cria a própria conta em `/registrar`.
2. "+ Novo lançamento" para lançar uma despesa.
3. O "Resumo" (página inicial) mostra os totais atualizados na hora.
4. Um administrador pode desativar contas em "Usuários".
