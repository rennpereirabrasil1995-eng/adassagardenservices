# Gestão de Jardinagem

App para você tocar sua empresa de jardinagem: agenda de trabalhos, clientes e
equipe, cada funcionário com o próprio login. O código é seu — pode editar
qualquer coisa aqui dentro, não depende de nenhum serviço pago.

- **Você (dono)** vê tudo: painel do dia, agenda completa, clientes e equipe.
- **Funcionário** só vê os trabalhos escalados para ele: endereço, como
  entrar, checklist de tarefas. Não vê suas anotações internas de cliente
  nem os trabalhos dos outros.

Stack: Python (Flask) + SQLite. Sem JavaScript pesado, sem build, sem conta
em nada externo. Se você não mexe com código no dia a dia, ainda dá pra achar
as coisas — é tudo HTML e CSS direto.

## Rodando no seu computador

Precisa ter o **Python 3.10 ou mais novo** instalado.

```bash
cd jardim-app
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Abre em **http://127.0.0.1:5000**.

Pra testar no celular sem publicar nada, com o celular na mesma rede Wi-Fi do
computador:

```bash
HOST=0.0.0.0 python run.py
```

E acessa pelo IP do computador na rede, tipo `http://192.168.0.15:5000`
(o IP você vê nas configurações de Wi-Fi do computador).

## Primeiro acesso

Da primeira vez que alguém abrir o app, ele cai direto em **"Criar conta do
dono"**. Só acontece uma vez — depois disso essa tela some sozinha e vira
login normal.

Depois de criar sua conta:
1. Cadastre seus clientes (**Clientes → Novo cliente**).
2. Cadastre sua equipe (**Equipe → Adicionar pessoa**) — o app sugere uma
   senha inicial pra cada um; anote e passe pra pessoa. Ela troca depois em
   **Conta**.
3. Comece a criar trabalhos (**Novo trabalho**, no painel ou na agenda).

## Minha conta

No topo, **Conta** abre um menu com um assunto por linha, e cada assunto tem a
sua página (com o caminho de volta pro menu):

- **Você** (todo mundo): **Senha** e **Idioma**. O dono também tem o **Perfil**
  (nome e telefone).
- **Empresa** (só o dono): **Dados da empresa**, **Aparência** (nome no topo,
  logo e cores), **Avisos por e-mail** (Gmail, e-mail de teste e link diário),
  **Espaço das fotos** e **Relatório em PDF**.
- No **Idioma**, cada pessoa escolhe o seu; o dono também escolhe o padrão da
  equipe (pra quem não escolheu).
- Cada linha do menu já mostra o que está valendo (a paleta, se os avisos por
  e-mail estão ligados, quanto espaço as fotos ocupam).
- No fim do menu tem o botão **Sair**. O endereço antigo `/preferencias` leva
  pra cá.

## O que mexer pra fazer o quê

| Quero...                                         | Mexo em...                                    |
|---------------------------------------------------|------------------------------------------------|
| Mudar o nome no topo, o logo ou as cores           | No próprio app: **Conta → Aparência** (só o dono) |
| Mudar as cores originais (paleta Floresta)         | `jardim/static/style.css`, bloco `:root` no topo |
| Mudar o texto de uma tela                          | `jardim/templates/*.html` (procure o texto e troque) |
| Mudar uma mensagem de erro                         | `jardim/__init__.py` → dicionário `ERROR_MESSAGES` |
| Mudar quem pode ver/fazer o quê                    | `jardim/auth.py` (`login_required`, `owner_required`) e o início de cada rota nos blueprints |
| Adicionar um campo novo (ex.: WhatsApp do cliente) | `jardim/schema.sql` (a coluna) + `jardim/clients.py` (salvar) + template correspondente |

As cores ficam todas em variáveis no topo do CSS, por exemplo:

```css
:root {
  --marker: #f4c430;   /* amarelo: ação principal e "em andamento" */
  --ink: #12301f;      /* verde escuro: texto e cabeçalho */
  --ok: #2f7d46;        /* verde: concluído */
  --late: #a8324a;      /* vermelho: atrasado, excluir */
}
```

Troca o valor em hexadecimal e o app inteiro muda — não precisa caçar cor
espalhada pelo código. (Pra trocar as cores sem mexer no código, use
**Conta → Aparência**: as paletas de lá substituem essas variáveis.)

## Configurar (arquivo `.env`)

Copie `.env.example` para `.env` e ajuste o que quiser (tudo é opcional, o
app funciona sem `.env` nenhum):

```bash
cp .env.example .env
```

Os campos mais importantes:

- `APP_NAME` / `APP_SHORT_NAME` — nome completo e nome curto (embaixo do
  ícone quando instala no celular). Só valem enquanto não houver nome em
  **Conta → Aparência** nem nome da empresa.
- `APP_TIMEZONE` — fuso horário usado pra "hoje", `Europe/London` já vem
  configurado.
- `SETUP_KEY` — se preencher, a tela de criar a conta do dono passa a pedir
  esse código. Use isso quando colocar o app online, senão qualquer pessoa
  que ache a URL antes de você pode criar a conta de dono primeiro.
- `SECRET_KEY` — chave interna de segurança. Local, pode deixar em branco (o
  app cria uma sozinha). Online, defina uma fixa (veja a seção de deploy).

## Colocando online

O `Procfile` já está pronto pra hospedagens tipo Render, Railway ou
PythonAnywhere (qualquer uma que rode `gunicorn`).

**Ponto de atenção, importante:** o banco de dados é um arquivo único
(`instance/jardim.db`). Muita hospedagem grátis apaga o disco a cada
atualização — se isso acontecer, você perde todos os clientes e trabalhos
cadastrados. Confirme que o plano tem **disco persistente** (às vezes chamado
de "volume" ou "persistent disk") apontando pra pasta `instance/` antes de
cadastrar clientes de verdade.

Ao publicar, configure estas variáveis de ambiente no painel da hospedagem:

- `SECRET_KEY` — uma string longa e aleatória (ex.: gere com
  `python3 -c "import secrets; print(secrets.token_hex(32))"`).
- `SETUP_KEY` — um código só seu, pra proteger a criação da conta de dono.
- `COOKIE_SECURE=1` — exige HTTPS pro cookie de login (toda hospedagem séria
  já dá HTTPS de graça).
- `BEHIND_PROXY=1` — quase toda hospedagem coloca um proxy na frente do seu
  app; isso é o que faz o app entender o HTTPS corretamente.

Depois do primeiro deploy, acesse a URL uma vez pra fazer o `/setup` com o
`SETUP_KEY` que você definiu.

## Atualizando o app já publicado

Quando eu (Claude) mudar alguma coisa no código e te mandar os arquivos de
novo, é assim que você leva a atualização pro site no ar, sem perder nada do
banco de dados:

1. No PythonAnywhere, aba **Files**, envie o(s) arquivo(s) novos por cima
   dos antigos (mesma pasta, `jardim-app/`), substituindo.
2. Abra um console **Bash** e rode:
   ```bash
   cd ~/jardim-app
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   Isso instala qualquer biblioteca nova que a atualização precise. Se nada
   mudou nas dependências, esse comando não faz nada de mal — pode rodar
   sempre, por garantia.
3. Antes do Reload, faça o backup: `python backup.py` (o banco só é atualizado no Reload).
4. Aba **Web** → botão verde **Reload**.

Mudanças na estrutura do banco (uma coluna nova, por exemplo) entram
sozinhas no passo 4 (Reload): o app confere e ajusta o banco automaticamente toda
vez que reinicia, sem apagar o que já está cadastrado. Você não precisa
mexer no banco na mão.

## Avisos automáticos para a equipe

Funciona sozinho, sem configurar nada a cada trabalho. O funcionário recebe um
aviso no sininho do topo do app quando:

- um trabalho é agendado pra ele (ou passado pra ele);
- o trabalho dele é remarcado, cancelado, excluído ou passado pra outra pessoa;
- várias repetições são criadas de uma vez (um aviso só, não um por cópia);
- é véspera de um trabalho dele (lembrete).

**Por e-mail (opcional):** em **Conta → Avisos por e-mail**, marque
"Mandar os avisos também por e-mail" e preencha:

1. Um Gmail que vai *enviar* os e-mails (pode ser o seu).
2. Uma **senha de app** desse Gmail — não é a senha normal. Precisa ter a
   verificação em duas etapas ligada na conta Google; depois crie a senha em
   https://myaccount.google.com/apppasswords (são 16 letras).
3. Salve e use o botão **Mandar e-mail de teste**.

No PythonAnywhere grátis, o Gmail é o único servidor de e-mail liberado.

**Quando saem os e-mails:** o de trabalho agendado/mudado sai na hora (depois
que a página já carregou, então ninguém espera). Os lembretes de véspera saem
uma vez por dia, na primeira visita ao site depois das 17h (mude com
`DAILY_EMAIL_HOUR` no `.env`). Contas grátis novas do PythonAnywhere não têm
tarefa agendada, então, pra garantir o horário mesmo sem ninguém abrir o site,
cadastre o **link diário** (fica em Conta → Avisos por e-mail) num serviço grátis como o
cron-job.org, pra ser aberto todo dia às 18h.

## Trabalho com várias pessoas (Share)

Ao agendar (ou em **Editar**), marque quem vai fazer o trabalho: uma pessoa, ou
várias para um trabalho **Share**. Sem ninguém marcado, fica para decidir depois.

- Cada pessoa marcada vê o trabalho em **Meus trabalhos**, pode marcar as tarefas,
  iniciar, concluir e mandar fotos, e recebe os avisos (inclusive o lembrete da
  véspera). Quem sai do trabalho numa edição recebe o aviso de que saiu.
- **Horas**: o tempo do trabalho conta para cada pessoa que foi (3 pessoas em 2h
  somam 6h de trabalho no relatório).
- **Dinheiro recebido do cliente**: fica com quem anotou o valor no app — é essa
  pessoa que aparece em "a repassar".
- Na agenda e no painel, trabalho Share aparece com os nomes e a etiqueta
  **Share**.

## Lembrete pro cliente (na véspera)

**Conta → Lembrete pro cliente** (só o dono). Na véspera de cada trabalho, o
cliente recebe uma mensagem confirmando a visita: um lembrete por cliente por
dia, com o horário mais cedo. Dois jeitos de mandar:

- **Com um toque (grátis):** o Painel mostra "Lembretes pra amanhã". Na lista,
  cada cliente tem o botão do **WhatsApp** e o do **SMS**: o celular abre a
  mensagem pronta, ela sai do seu número e a resposta chega pra você. O app
  anota quem já foi avisado.
- **SMS automático (Twilio, pago):** sai sozinho na véspera, a partir da hora
  escolhida (padrão 18h) e até as 21h. Custa por volta de 4p por SMS no Reino
  Unido (cobrado pelo Twilio). Funciona no PythonAnywhere grátis (o
  `api.twilio.com` está na lista liberada). Pra configurar:
  1. Crie uma conta em twilio.com e faça o upgrade colocando crédito (a conta
     de teste só manda pra números verificados lá e só com textos prontos do
     Twilio).
  2. No Console do Twilio, copie o **Account SID** e o **Auth Token**.
  3. Escolha o remetente: um nome de até 11 letras, tipo `RenanGarden` (grátis;
     o cliente não consegue responder pro nome, mas a mensagem já leva o seu
     telefone) ou um número do Twilio (+44…, uns US$ 2,50 por mês).
  4. Cole tudo em Conta → Lembrete pro cliente, escolha **SMS automático** e
     use **Mandar SMS de teste**.
  5. Recomendado: cadastre o **link diário** (Conta → Avisos por e-mail) no
     cron-job.org às 18h, pra os SMS saírem na hora mesmo que ninguém abra o app.
- A mensagem dá pra editar (inglês ou português), com prévia e contador de SMS.
  Acentos como á, ã e ç fazem cada SMS caber só 70 letras (e custar mais).
- Pra deixar um cliente de fora (um prédio com porteiro, por exemplo),
  desmarque **Lembrete na véspera** no cadastro dele.
- SMS que falhou (número errado, sem crédito) aparece na lista com o motivo: dá
  pra mandar de novo ou usar o WhatsApp.

## Minhas horas (só consulta)

Aba **Minhas horas** (todo mundo tem, inclusive o funcionário): quanto a pessoa
trabalhou no **dia**, na **semana**, no **mês** ou no **ano**, com as setas pra
voltar aos períodos anteriores.

- **Resumo**: horas trabalhadas, trabalhos concluídos, média por dia e clientes
  (no dia: a hora em que começou e a hora em que terminou).
- **Gráficos**: horas por dia (semana e mês) ou por mês (ano), o dia em linha do
  tempo (do Iniciar ao Concluir de cada trabalho) e horas por cliente. Tocar numa
  coluna abre aquele dia (ou mês).
- **Tabelas**: semana a semana (no mês), mês a mês (no ano) e a lista dos
  trabalhos com início, fim e duração.
- **Só pra ver**: as horas vêm do Iniciar e do Concluir de cada trabalho.
  Ninguém digita nem troca horas nessa tela, e cada pessoa só vê as próprias.
- Trabalho ainda em andamento aparece num aviso (as horas entram quando concluir).
  Trabalho concluído sem tocar em Iniciar aparece como "sem horário de início" e
  fica sem horas.
- A conta é a mesma do relatório da empresa: trabalho concluído, no dia em que
  estava agendado; em Share, o tempo conta inteiro pra cada pessoa que foi.

## Cotações (orçamento por link)

Aba **Cotações** (você e os gerentes que tiverem esse acesso):

1. **Nova cotação**: pra quem (cliente cadastrado ou pessoa nova), o que vai
   ser feito, os itens com preço (sem preço = "incluído"), frequência (uma vez,
   toda semana, a cada 15 dias, todo mês), validade e observações.
2. Na tela da cotação, adicione as **fotos da visita**. Embaixo de cada foto tem
   um campo pra escrever sobre ela; o texto salva sozinho.
3. **Mandar pelo WhatsApp** abre o WhatsApp com a mensagem pronta e o link.
   **Mandar por e-mail** usa o Gmail configurado em Conta → Avisos por e-mail. Ou copie o link.
4. O cliente abre o link **sem login**: vê as fotos com os textos, os itens, o
   total e toca em **Aceitar** (digitando o nome) ou **Recusar**.
5. Você recebe um aviso quando ele abre e quando responde. Aceitou: **Agendar o
   trabalho** cadastra o cliente (se for novo) e abre o trabalho com os itens
   como tarefas e os textos das fotos como instruções.

Sobre o link: é longo e aleatório (ninguém chega nele sem ter recebido), não
aparece em buscador e mostra só a cotação, nunca anotação interna nem código
de portão. **Cancelar cotação** desliga o link na hora (dá pra reabrir).

## Área da empresa (relatório com fotos, sem WhatsApp)

Pra empresa que contrata você pra vários jardins e quer ver o que foi feito em
cada um. Em vez de mandar as fotos e o relatório pelo WhatsApp todo dia, a
pessoa responsável entra no app com o login dela e vê tudo sozinha.

**Clientes → Empresas** (só o dono):

1. **Cadastrar empresa** (ex.: Hillside Property Services).
2. **Ligar um jardim**: cada jardim é um cliente seu. Dá pra ligar os que já
   existem, cadastrar um novo direto pra empresa, ou escolher a **Empresa** no
   cadastro do cliente.
3. **Criar acesso**: nome, e-mail e o idioma da área dela (o app sugere a
   senha). Na tela seguinte vem a mensagem pronta pra **Mandar pelo WhatsApp**
   (endereço, e-mail e senha). A senha só aparece nessa tela; esqueceu, é só
   tocar em **Nova senha**.
4. **Ver como a empresa vê** mostra a área dela exatamente como ela vê.

O que ela vê (só vê, não muda nada): os serviços **concluídos** dos jardins da
empresa, **dia a dia** (com o total de horas no local) ou **jardim por
jardim**, com o horário de Iniciar e Concluir, as tarefas feitas e as não
feitas (com o motivo), os **materiais** e as fotos de **Antes** e **Depois**.
Nunca vê valores, telefone, anotações ou código de portão do cliente, o recado
da equipe, nem quem da equipe foi. O que a equipe digita (materiais, motivo)
aparece do jeito que foi escrito: nos jardins da empresa, escreva em inglês.

**Comentários**: em cada serviço ela pode comentar. O comentário chega no seu
sininho (e no do gerente com a agenda) e, com os avisos por e-mail ligados,
por e-mail. Você responde na página do trabalho, em **Comentários**; a
resposta aparece na área dela como **nova** e vai por e-mail pra ela. A
funcionária não vê a conversa.

Os dois lados podem **anexar fotos e documentos** no comentário (**Anexar foto
ou arquivo**): até 6 por comentário, fotos de qualquer tamanho (o celular reduz
antes de enviar, e sai a localização) e PDF, Word ou Excel de até 10 MB. Antes
de enviar aparecem as miniaturas, com um × pra tirar. Na conversa, a foto abre
grande e o PDF abre no navegador (Word e Excel baixam). Os anexos contam no
espaço das fotos; apagar o comentário apaga os arquivos.

O login dela é na mesma tela de login do app; ela cai direto na área da
empresa e nenhuma outra tela abre. **Desativar** tira o acesso na hora.

**Resumo do dia por e-mail**: na página da empresa, escolha o horário (padrão
18h) ou desligue. Uma vez por dia, a partir desse horário e até as 22h, cada
pessoa da empresa recebe um e-mail com os serviços concluídos desde o último
resumo (horário, tarefas, as não feitas com o motivo, materiais, quantas fotos)
e o link pra área. Sem serviço novo, não sai nada; o que ficar pronto depois vai
no do dia seguinte.

- Sai pelo Gmail de **Conta → Avisos por e-mail** (sem ele, nada sai).
- A página da empresa mostra como vai ficar o próximo e tem **Mandar agora**.
- Cada pessoa da empresa pode desligar na conta dela.
- Sai na primeira visita ao app depois do horário (de qualquer pessoa), ou na
  hora certa se o **link diário** estiver no cron-job.org: o mesmo link dos
  avisos e do lembrete pro cliente. Com o cron às 18h, cobre os três.

## Aparência (nome, logo e cores)

**Conta → Aparência** (só o dono). Vale pra equipe toda e pra página de
cotação que o cliente abre.

- **Nome no topo**: em branco, aparece o nome da empresa. **Nome curto**: o que
  fica embaixo do ícone do app no celular (até 12 letras).
- **Logo**: PNG ou JPG (com fundo transparente fica melhor). Ele vai pro topo do
  app, pra página da cotação, pra aba do navegador e vira o ícone do app no
  celular (gerado sozinho, com fundo na cor do topo). Se o logo já tem o nome
  escrito, desmarque **Mostrar o nome ao lado do logo**.
- **Cores**: 9 paletas prontas ou **Minhas cores** (a cor do topo e a dos botões).
  Fundo, texto, links e o modo escuro inteiro são calculados a partir delas,
  sempre conferindo o contraste. Se a cor do topo for um tom médio (cinza, vermelho
  vivo...), aparece um aviso: o texto lá em cima fica difícil de ler no sol.
- A prévia (modo claro e escuro) muda na hora, antes de salvar. **Voltar ao
  original** volta nome e cores do começo (o logo fica; pra tirar, marque
  **Tirar o logo**).
- Celular com o app instalado: se o ícone não mudar sozinho depois de trocar o
  logo, remova o app da tela inicial e adicione de novo.
- Impressão e PDF continuam em preto no branco.

## Fotos (Antes e Depois)

Cada trabalho tem duas seções de fotos, **Antes** e **Depois**, com até 12 fotos
cada. O cliente tem ainda a galeria geral dele (até 12), na tela do cliente.

- **Tirar foto** abre a câmera; **Da galeria** deixa escolher várias de uma vez.
- O próprio celular reduz cada foto antes de enviar (uma foto de 4 MB sobe com
  poucas centenas de KB), e elas vão uma por vez, aparecendo na tela conforme
  chegam. Se a internet cair no meio, aparece **Tentar de novo** só com as que
  faltaram.
- No servidor cada foto fica com no máximo 1600 px, sem a localização GPS que o
  celular grava dentro dela, mais uma miniatura pequena pra tela carregar rápido.
- Funcionário envia nos trabalhos dele e apaga só as fotos que ele mesmo enviou.
  Quem tem acesso à agenda (você e gerentes com esse acesso) apaga qualquer uma.
- Excluir um trabalho apaga as fotos dele junto.

**Espaço em disco.** O PythonAnywhere grátis tem 512 MB no total, e o app com as
bibliotecas já usa parte disso. Por isso as fotos têm um teto: **300 MB** (em
Conta → Espaço das fotos você vê quanto já foi usado). Quando chega no
teto, o app para de aceitar fotos novas até você apagar algumas; o resto
continua funcionando normalmente. Se mudar para um plano com mais disco, aumente
o teto colocando esta linha no arquivo WSGI (aba Web), junto das outras
`os.environ`, e dê Reload:

```python
os.environ['PHOTO_SPACE_MB'] = '4000'
```

As fotos ficam em `instance/uploads/` e não fazem parte do `backup.py` (que copia
só o banco). Pra guardar uma cópia das fotos no seu computador, rode no console
Bash `cd ~/jardim-app && zip -r ~/fotos.zip instance/uploads`, baixe o
`fotos.zip` pela aba Files e depois apague ele de lá (ele ocupa o mesmo espaço
que as próprias fotos).

## Backup do banco de dados

O banco inteiro fica em `instance/jardim.db`. Não baixe só esse arquivo: o banco usa o modo WAL, e as
alterações mais recentes ficam num arquivo separado (`jardim.db-wal`) até serem juntadas. Pra ter uma
cópia completa, rode no console Bash, dentro da pasta `jardim-app` (com o site no ar mesmo):

```bash
python backup.py
```

A cópia vai pra pasta `backups/`, com a data e a hora no nome. Pra guardar no seu computador:
aba Files > `jardim-app` > `backups` > ícone de download. Faça isso antes de cada atualização.

## Esqueci a senha de alguém

Alguém da área de uma empresa: **Clientes → Empresas → Nova senha** (a tela
mostra a senha nova e a mensagem pronta pro WhatsApp).

Sem tela de "esqueci a senha" por e-mail (o app não manda e-mail). Pra
resetar a senha de alguém direto no banco, rode isto na pasta do projeto
(com o ambiente virtual ativado):

```bash
python3 -c "
from jardim import create_app, auth
from jardim.db import get_db
app = create_app()
app.app_context().push()
db = get_db()
db.execute('UPDATE users SET password_hash = ? WHERE email = ?',
           (auth.hash_password('nova-senha-aqui'), 'email-da-pessoa@exemplo.com'))
db.commit()
print('senha trocada')
"
```

Troque `nova-senha-aqui` e `email-da-pessoa@exemplo.com` antes de rodar.

## Instalar no celular como um app

Com o app aberto no navegador do celular:

- **Android (Chrome)**: menu (⋮) → "Instalar aplicativo" ou "Adicionar à
  tela inicial".
- **iPhone (Safari)**: ícone de compartilhar → "Adicionar à Tela de Início".

Fica com ícone próprio (o seu logo, se tiver um em **Conta →
Aparência**) e abre em tela cheia, sem barra de endereço. Funciona
melhor depois de colocado online com HTTPS — local (`http://`), alguns
celulares não oferecem a opção de instalar.

## Rodar os testes automáticos

Sempre que mexer no código, vale rodar os testes pra ver se não quebrou nada:

```bash
python -m unittest
```

Os testes cobrem login, permissões (funcionário não acessa o que não
deveria), checklist de tarefas, repetição de trabalhos e mais. Se algo
quebrar, a mensagem de erro aponta o teste e o motivo.

## Estrutura de pastas

```
jardim-app/
├── run.py                  # ponto de entrada: python run.py
├── requirements.txt
├── .env.example            # copie para .env
├── Procfile                # deploy com gunicorn
├── jardim/
│   ├── __init__.py         # cria o app, liga tudo
│   ├── auth.py             # login, setup, permissões
│   ├── clients.py          # cadastro de clientes
│   ├── team.py             # equipe
│   ├── jobs.py             # agenda, trabalhos, tarefas
│   ├── hours.py            # minhas horas: resumo e gráficos de cada pessoa (só consulta)
│   ├── reminders.py        # lembrete pro cliente na véspera (WhatsApp/SMS com um toque ou SMS pelo Twilio)
│   ├── notifications.py    # avisos automáticos (app e e-mail)
│   ├── photos.py           # fotos: galeria do cliente, Antes/Depois e fotos das cotações
│   ├── quotes.py           # cotações: montar, mandar o link e receber a resposta do cliente
│   ├── companies.py        # empresas com vários jardins (lado do dono): jardins, logins e prévia
│   ├── portal.py           # área da empresa: serviços concluídos com fotos, e os comentários
│   ├── portal_mail.py      # resumo do dia por e-mail pra empresa
│   ├── branding.py         # aparência: nome no topo, logo, ícones e paletas de cores
│   ├── db.py                # conexão com o banco
│   ├── utils.py            # datas e validações
│   ├── schema.sql          # estrutura do banco
│   ├── templates/          # as telas (HTML)
│   └── static/             # CSS, fontes, ícones e photos.js (envio das fotos)
├── tests/
│   └── test_app.py         # os testes automáticos
└── instance/                # criado sozinho: banco de dados e fotos (uploads/) ficam aqui
```
