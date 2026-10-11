# Dashboard de Absenteísmo — Empresa Exemplo

Aplicação web "tradicional": **HTML + CSS + JavaScript no navegador** e
**Python (Flask)** no servidor — sem framework de dashboard (nada de
Streamlit). O Python só entra como backend: autenticação, banco de dados e
uma API JSON; toda a tela é HTML/CSS/JS servido e renderizado pelo
navegador.

## Visão geral

1. **Login obrigatório antes de tudo** — `app.py` (`login_requerido`)
   Nenhuma rota de dados responde sem uma sessão autenticada. A primeira
   coisa que qualquer pessoa vê ao abrir o link é a tela de login (ou, na
   primeiríssima execução, a tela de criação da conta de desenvolvedor).

2. **Três níveis de acesso** — `database.py`
   Cada usuário tem um `cargo`: `dev` (quem instala/mantém o sistema — a
   primeira conta criada), `admin` (delegado pelo dev para cadastrar e
   gerenciar os demais usuários) ou `usuario` (só visualiza o dashboard).
   Só um `dev` pode criar outro `dev` ou desativar uma conta `dev` — um
   `admin` não consegue se autopromover nem mexer em contas de nível mais
   alto que a sua (checado tanto no servidor quanto escondido na tela).

3. **Senhas com Argon2id** — `crypto_utils.py`
   Senhas nunca são guardadas em texto puro nem com hash reversível.
   Usamos Argon2id (vencedor da *Password Hashing Competition*, recomendado
   pela OWASP), via `argon2-cffi`. O algoritmo é propositalmente "caro" em
   tempo e memória — isso dificulta ataques de força bruta mesmo que o
   banco vaze.

4. **Banco de dados criptografado em disco (AES-256)** — `database.py`
   O arquivo `absenteismo.db` é criptografado com **SQLCipher**
   (`sqlcipher3-wheels`, com binários pré-compilados para Windows, macOS e
   Linux), extensão do SQLite que aplica AES-256 página a página. O arquivo
   `.db` sozinho é inútil sem a chave — mesmo copiado do disco ou de um
   backup — e os backups saem automaticamente criptografados também.

5. **Sessão de servidor assinada** — `app.py` / `crypto_utils.py`
   O login usa o mecanismo de sessão do Flask: um cookie assinado (HMAC)
   com uma `SECRET_KEY` de 256 bits (gerida do mesmo jeito que a chave do
   banco — ver abaixo). Sem essa assinatura, alguém poderia adulterar o
   próprio cookie no navegador e se passar por outro usuário. O cookie é
   `HttpOnly` (JavaScript não consegue lê-lo) e `SameSite=Lax`.

6. **Proteção contra CSRF** — `app.py`
   Toda ação que muda dados (importar planilha, criar backup, criar/alterar
   usuário) exige um token CSRF gerado no login e conferido no servidor
   (`csrf_requerido`). Sem o token certo, a requisição é recusada com 400 —
   isso impede que outro site force o navegador da vítima a disparar essas
   ações usando sua sessão sem que ela perceba.

## Estrutura dos arquivos

```
app.py                  -> aplicação Flask: rotas de página + API JSON
database.py              -> acesso ao banco (usuários, dados, backup)
crypto_utils.py           -> hash de senha (Argon2id), chave do banco e secret key de sessão
excel_import.py            -> leitura/parse da planilha .xlsx enviada
templates/
    base.html               -> layout comum (sidebar + área de conteúdo)
    login.html                -> tela de login / criação da 1ª conta (dev)
    dashboard.html              -> conteúdo do dashboard (estende base.html)
static/
    css/style.css               -> identidade visual única (roxo/magenta, responsivo)
    js/login.js                  -> pequeno detalhe da tela de login
    js/main.js                    -> sidebar mobile/desktop
    js/dashboard.js                -> busca dados da API, gráficos (Chart.js), filtros, upload, backup, usuários
    js/vendor/chart.umd.js          -> Chart.js 4.4.1 servido localmente (funciona sem internet)
requirements.txt          -> dependências Python
Relatorio_de_Absenteismo.xlsx (opcional) -> planilha usada para popular o banco na 1ª execução
```

## Como a planilha é lida (correções automáticas)

A planilha de origem tem alguns problemas conhecidos. `excel_import.py`
trata cada um na importação e mostra um aviso na tela dizendo o que ajustou:

- **% de absenteísmo recalculado**: sempre Horas Ausente ÷ Horas Previstas
  × 100. O valor da célula não é usado (em Julho/Agosto o total da planilha
  divide por horas trabalhadas, e a célula 18:30 da Júlia em Junho virava
  0,4% em vez de 10%). O card "% Absenteísmo geral" soma as horas do
  período (não é média dos percentuais).
- **Horas em "hh.mm"**: na planilha, 24.33 quer dizer 24h33min. Por linha,
  o sistema escolhe a leitura que faz trabalhadas + ausentes = previstas.
  Para desligar, mude `HORAS_NO_FORMATO_HH_MM` para `False`.
- **Aba copiada**: uma aba mensal idêntica a outra (hoje "Setembro 26" é
  cópia de "Agosto 26") é ignorada. Bancos antigos que já tinham importado
  essa cópia são limpos na inicialização, com backup automático antes.
- **Nomes diferentes da mesma pessoa** entre abas são unificados (ex.:
  "Maria Souza Lima" → "Maria Aparecida Souza Lima").
- **Linhas onde as horas não fecham** (previstas ≠ trabalhadas + ausentes)
  são listadas no aviso para conferência.
- **Reimportar substitui o mês**: os meses que vierem na planilha são
  regravados por completo (quem saiu da planilha não fica no banco); meses
  que não vierem continuam como estavam.

## Gestão das chaves (banco + sessão)

Duas chaves de 256 bits, cada uma resolvida em `crypto_utils.py` nesta
ordem de prioridade:

| Chave | Variável de ambiente | Arquivo local (auto-criado) | Para quê |
|---|---|---|---|
| Banco de dados | `ABSENTEISMO_DB_KEY` | `db.key` | Criptografa `absenteismo.db` (SQLCipher/AES-256) |
| Sessão (Flask) | `ABSENTEISMO_SECRET_KEY` | `secret.key` | Assina o cookie de login |

**Importante:** se a chave do banco for perdida, os dados em `absenteismo.db`
(e em qualquer backup feito a partir dele) ficam irrecuperáveis. Se a chave
de sessão mudar, todo mundo é deslogado (mas nenhum dado é perdido). Guarde
cópias de `db.key` e `secret.key` em lugar separado do servidor (ex.:
gerenciador de senhas da empresa). Ambos os arquivos, junto com
`absenteismo.db` e `backups/`, já estão no `.gitignore`.

Para definir as chaves via variável de ambiente no Windows (PowerShell),
antes de rodar o app:
```powershell
$env:ABSENTEISMO_DB_KEY = "chave-hexadecimal-de-64-caracteres"
$env:ABSENTEISMO_SECRET_KEY = "outra-chave-hexadecimal-de-64-caracteres"
python app.py
```

## Como rodar

```bash
pip install -r requirements.txt
python app.py
```

Abra `http://127.0.0.1:5000` no navegador.

Na primeira execução:
1. A tela pedirá para criar a conta de desenvolvedor — cargo `dev`, acesso
   total (nome, login, senha — mínimo de 8 caracteres). É a conta de quem
   instala/mantém o sistema.
2. Depois de criada, faça login normalmente.
3. Se existir `Relatorio_de_Absenteismo.xlsx` na pasta, os dados já aparecem
   carregados. Caso contrário, envie a planilha pelo painel lateral
   ("Atualização de Dados").
4. No painel "Gerenciar Usuários" da barra lateral, o `dev` cadastra a(s)
   conta(s) `admin` (quem também vai gerenciar usuários no dia a dia) e as
   contas `usuario` (só veem o dashboard) — cada pessoa com seu próprio
   login. Um `admin` só consegue criar contas `admin`/`usuario`, nunca `dev`
   (o servidor recusa a tentativa mesmo que alguém force a chamada da API).

## Deploy na VPS (Docker + Nginx Proxy Manager)

Passo a passo completo, do Windows até o site no ar: [DEPLOY.md](DEPLOY.md).

O Nginx Proxy Manager encaminha `absenteismo.exemplo.com.br` para
`http://Absenteismo:8501`. O `docker-compose.yml` já sobe o container com
esse nome e nessa porta, usando gunicorn.

Na VPS:

```bash
git clone https://github.com/carloshenriquedevcombr/nexusrh-absenteismo.git absenteismo
cd absenteismo
cp .env.example .env          # ajuste PROXY_NETWORK (rede do Nginx Proxy Manager)
mkdir -p dados
# Para manter o banco atual, copie do seu computador para dados/:
#   absenteismo.db, db.key e secret.key   (scp, WinSCP...)
docker stop Absenteismo && docker rm Absenteismo   # só se o container antigo (Streamlit) ainda existir
docker compose up -d --build
docker compose logs -f
```

Para atualizar depois de um novo push:

```bash
git pull && docker compose up -d --build
```

Tudo que precisa sobreviver às atualizações (banco, backups, chaves) fica
em `dados/`, fora da imagem e fora do Git. Faça cópia dessa pasta.

### Variáveis de ambiente

| Variável | Para quê |
|---|---|
| `ABSENTEISMO_DATA_DIR` | Pasta do banco, backups e chaves (no Docker: `/app/dados`) |
| `ABSENTEISMO_HTTPS=1` | Cookie de sessão só por HTTPS |
| `ABSENTEISMO_ATRAS_DE_PROXY=1` | Confia nos cabeçalhos `X-Forwarded-*` do proxy |
| `ABSENTEISMO_DB_KEY` / `ABSENTEISMO_SECRET_KEY` | Chaves (opcional; senão usa os arquivos em `dados/`) |

Para rodar em produção fora do Docker, use um servidor WSGI de verdade em vez do servidor
de desenvolvimento do Flask, por exemplo:
```bash
pip install gunicorn   # Linux/macOS
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```
No Windows, `waitress` é a alternativa mais comum (`pip install waitress`,
depois `waitress-serve --port=8000 app:app`).

### Se a instalação do `sqlcipher3-wheels` falhar

Esse pacote traz binários pré-compilados (SQLCipher 4) para Windows, macOS e
Linux, em praticamente todas as versões atuais do Python (3.8–3.14), então
`pip install -r requirements.txt` deve resolver sozinho na maioria dos casos.
Se mesmo assim falhar:
- Confirme sua versão com `python --version` e atualize o `pip` antes
  (`python -m pip install --upgrade pip`).
- Alternativa: instalar via conda
  (`conda install -c conda-forge sqlcipher3`).

> **Observação técnica:** os pacotes `sqlcipher3-binary` (só Linux) e
> `sqlcipher3-wheels` (Windows/macOS/Linux) expõem o mesmo módulo Python
> (`sqlcipher3`), então `database.py` não muda dependendo de qual dos dois
> está instalado — só o `requirements.txt` aponta para o que tem wheel para
> Windows.

## Backup e recuperação

- `absenteismo.db` é o banco "vivo", já criptografado em disco.
- `backups/absenteismo_backup_AAAAMMDD_HHMMSS.db` são cópias pontuais criadas
  ao clicar em "Criar backup agora" na barra lateral — também criptografadas
  — e podem ser baixadas dali mesmo.
- Para restaurar: pare o app, substitua `absenteismo.db` pelo backup desejado
  (renomeando-o para `absenteismo.db`) — usando a **mesma chave de banco**
  que estava em uso quando aquele backup foi feito — e inicie o app
  novamente.
- Guarde cópias do `absenteismo.db` (ou dos backups baixados) e do `db.key`
  fora do computador/servidor onde o app roda, em locais **separados um do
  outro**.

## Arquitetura front-end (por que sem framework de UI)

`dashboard.html`/`login.html` são HTML simples servidos pelo Flask
(Jinja2); `static/css/style.css` define toda a identidade visual num só
lugar; `static/js/dashboard.js` busca os dados prontos da API
(`/api/dashboard`, `/api/opcoes`, etc., todas devolvendo JSON) e desenha os
gráficos com **Chart.js** (servido localmente de `static/js/vendor/`, sem depender de internet). Não existe nenhuma camada
extra de renderização entre o CSS escrito e o que o navegador mostra — o
que elimina de raiz a classe de bug que existia na versão anterior
(baseada em Streamlit), onde uma técnica de injeção de CSS podia falhar de
formas imprevisíveis dependendo de como o framework processava o conteúdo.

Responsivo (Android/iOS/desktop): a barra lateral vira um menu
"hambúrguer" deslizante abaixo de 768px (`static/js/main.js` +
`static/css/style.css`), e os cards/gráficos empilham em uma coluna só.

## Notas de complexidade (Big O) e decisões de design

- `username` e `(colaborador, mes)` têm restrição `UNIQUE`, o que faz o
  SQLite manter um índice B-tree para cada um: login e upsert de dados
  buscam/gravam em **O(log n)**, não em uma varredura O(n) da tabela.
- A importação da planilha grava todas as linhas com `executemany` (uma
  viagem de I/O em lote) em vez de um `INSERT` por linha dentro de um loop
  Python — mesma complexidade assintótica (O(n log n) pelos índices), mas
  com um fator constante bem menor.
- A filtragem por mês/colaborador acontece em memória com `pandas` (O(n)
  por filtro) depois de uma única leitura do banco por requisição — para o
  volume de dados de uma folha de ponto mensal (centenas/poucos milhares de
  linhas), isso é mais simples e igualmente rápido do que reescrever cada
  filtro como uma consulta SQL separada.
- O hash de senha (Argon2id) é a exceção deliberada: seu custo é **alto de
  propósito** (tempo e principalmente memória), porque aqui o objetivo não é
  velocidade — é tornar caro para um atacante testar senhas em massa, mesmo
  que o banco de hashes vaze.

## Observações de segurança

- Troque a senha da conta `dev` inicial assim que possível e não a
  compartilhe.
- Nunca versionar `db.key`, `secret.key`, `absenteismo.db`, `backups/` ou
  qualquer `.xlsx` em um repositório (já cobertos pelo `.gitignore`).
- Perder a chave de criptografia do banco (`db.key`/`ABSENTEISMO_DB_KEY`)
  significa perder o acesso a todos os dados — trate-a com o mesmo cuidado
  de uma senha de administrador de banco.
- Em produção, sirva a aplicação atrás de HTTPS (por exemplo com um proxy
  reverso como Nginx/Caddy) — sem isso, o cookie de sessão trafega em texto
  claro na rede.
