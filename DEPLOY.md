# Passo a passo: colocar o sistema na VPS

Guia para subir o sistema de absenteísmo na VPS, atrás do Nginx Proxy
Manager que já encaminha `absenteismo.exemplo.com.br` para
`http://Absenteismo:8501`. Os comandos marcados como **PowerShell** rodam no
seu Windows; os marcados como **VPS** rodam no terminal da VPS (via SSH).

---

## 1. Entrar na VPS

**PowerShell**
```powershell
ssh usuario@IP_DA_VPS
```
Troque `usuario` e `IP_DA_VPS` pelos seus (os mesmos que você já usa para
acessar a VPS). A partir daqui, tudo é **VPS** até o passo 5.

## 2. Conferir o Docker

**VPS**
```bash
docker --version
docker compose version
```
Os dois precisam responder com uma versão. Se `docker compose` não existir,
instale o plugin: `sudo apt install docker-compose-plugin`.

## 3. Descobrir a rede do Nginx Proxy Manager

O container do sistema precisa estar na mesma rede Docker do proxy, senão o
site dá **502 Bad Gateway**.

**VPS**
```bash
docker ps --format '{{.Names}}'
```
Ache o nome do container do Nginx Proxy Manager (algo como `npm-app-1` ou
`nginx-proxy-manager`). Depois:
```bash
docker inspect NOME_DO_CONTAINER_NPM --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```
Anote o nome que aparecer (ex.: `npm_default`). Ele vai no passo 6.

## 4. Baixar o projeto do GitHub

O repositório é privado, então o `git clone` vai pedir login. No lugar da
senha, o GitHub exige um **token**:

1. No navegador: GitHub → foto do perfil → **Settings** → **Developer
   settings** → **Personal access tokens** → **Fine-grained tokens** →
   **Generate new token**.
2. Em *Repository access*, escolha **Only select repositories** →
   `nexusrh-absenteismo`.
3. Em *Permissions* → *Repository permissions* → **Contents: Read-only**.
4. Gere e copie o token (ele só aparece uma vez).

**VPS**
```bash
cd ~
git clone https://github.com/carloshenriquedevcombr/nexusrh-absenteismo.git absenteismo
cd absenteismo
```
Quando pedir *Username*, use `carloshenriquedevcombr`; em *Password*, cole o
token.

Para não precisar digitar o token de novo em toda atualização:
```bash
git config credential.helper store
```
(o próximo `git pull` pede o token uma última vez e guarda.)

## 5. Levar seu banco atual para a VPS

Se quiser manter os dados e usuários que você já tem, copie os três arquivos
do seu computador para a pasta `dados/` da VPS. **Se pular este passo, o
sistema começa vazio** e pede para criar a conta de dev.

**VPS** (cria a pasta):
```bash
mkdir -p ~/absenteismo/dados
```

**PowerShell** (numa janela nova, no seu computador):
```powershell
cd D:\projetos\Absenteismo
scp absenteismo.db db.key secret.key usuario@IP_DA_VPS:~/absenteismo/dados/
```

> `db.key` é o que abre o banco. Sem ela, o `absenteismo.db` é ilegível.
> Sempre copie os dois juntos.

## 6. Configurar o `.env`

**VPS**
```bash
cd ~/absenteismo
cp .env.example .env
nano .env
```
Na linha `PROXY_NETWORK=`, coloque o nome da rede que você anotou no passo 3.
Deixe as linhas das chaves vazias (o sistema usa os arquivos em `dados/`).
Salve com `Ctrl+O`, `Enter`, e saia com `Ctrl+X`.

## 7. Dar permissão na pasta de dados

Dentro do container o sistema roda com um usuário comum (id 1000), que
precisa conseguir gravar em `dados/` (banco e backups).

**VPS**
```bash
sudo chown -R 1000:1000 ~/absenteismo/dados
```

## 8. Tirar o container antigo (Streamlit)

O container antigo usa o mesmo nome (`Absenteismo`). Ele precisa sair antes.

**VPS**
```bash
docker ps -a --filter name=Absenteismo
```
Se aparecer algum, remova:
```bash
docker stop Absenteismo
docker rm Absenteismo
```

## 9. Subir o sistema

**VPS**
```bash
cd ~/absenteismo
docker compose up -d --build
```
A primeira vez demora alguns minutos (baixa a imagem do Python e instala as
dependências). Depois:
```bash
docker compose ps
docker compose logs -f
```
Nos logs deve aparecer `Booting worker with pid`. Saia dos logs com `Ctrl+C`
(o sistema continua rodando).

## 10. Testar

Abra `https://absenteismo.exemplo.com.br`. A tela de login deve
aparecer. Não precisa mexer no Nginx Proxy Manager: ele já aponta para
`Absenteismo:8501`.

---

## Atualizar depois de uma mudança no código

**VPS**
```bash
cd ~/absenteismo
git pull
docker compose up -d --build
```
Os dados em `dados/` não são afetados.

## Backup

Tudo que importa está em `~/absenteismo/dados/` (banco, chaves e backups
gerados pelo botão do sistema). Para baixar uma cópia para o seu computador:

**PowerShell**
```powershell
scp -r usuario@IP_DA_VPS:~/absenteismo/dados D:\backup-absenteismo
```

---

## Se der erro

| O que aparece | Causa provável | O que fazer |
|---|---|---|
| Site mostra **502 Bad Gateway** | Container fora da rede do proxy, ou parado | Confira `PROXY_NETWORK` no `.env` (passo 3), depois `docker compose up -d` |
| `network ... declared as external, but could not be found` | Nome da rede errado no `.env` | Refaça o passo 3 e corrija o `.env` |
| `Conflict. The container name "/Absenteismo" is already in use` | Container antigo ainda existe | Passo 8 |
| Logs mostram `Erro ao iniciar o banco de dados` / chave | `db.key` não é a mesma que criou o banco | Copie de novo `absenteismo.db` **e** `db.key` juntos (passo 5) |
| Logs mostram `Permission denied` em `/app/dados` | Pasta sem permissão | Passo 7 |
| `git clone` recusa a senha | GitHub não aceita senha, só token | Passo 4 |
| Login funciona mas desloga sozinho | Acesso por `http://` em vez de `https://` | Use sempre o endereço com `https://` |

Se nada disso resolver, rode `docker compose logs --tail 100` e mande a saída.
