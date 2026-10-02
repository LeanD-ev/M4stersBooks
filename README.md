# M4 Books

Catálogo Flask alimentado pelos nomes de arquivos `.epub` em uma pasta pública do Google Drive. O site lê somente a listagem da pasta; não baixa nem abre o conteúdo dos EPUBs para montar o catálogo. Para buscar os metadados, consulta Google Books primeiro e usa Open Library como alternativa quando não encontra uma correspondência confiável. O catálogo não depende de CSVs ou imagens de capa locais.

## Configurar o Google Drive

1. Na pasta do Drive, abra **Compartilhar** e defina **Acesso geral → Qualquer pessoa com o link → Leitor**.
2. No Google Cloud Console, selecione ou crie um projeto, ative a **Google Drive API** e crie uma chave de API.
3. Restrinja a chave à Google Drive API. Não publique a chave em repositório nem em páginas do site.
4. Configure `GOOGLE_DRIVE_API_KEY` no ambiente do processo Flask. `GOOGLE_DRIVE_FOLDER_ID` é opcional e, por padrão, usa a pasta informada para este projeto.
5. Coloque os arquivos EPUB diretamente na pasta compartilhada. O catálogo consulta novamente a pasta a cada cinco minutos.

A chave é usada para listar arquivos e nomes pela Drive API. Os downloads redirecionam o navegador ao link público do arquivo no Google Drive, sem passar o conteúdo pelo servidor do site. A pasta e os arquivos precisam permitir leitura a qualquer pessoa com o link. Metadados e capas vêm das fontes bibliográficas; um cache local opcional reduz consultas repetidas, mas não é necessário para executar o catálogo. A primeira leitura da pasta acontece em segundo plano; os títulos aparecem enquanto os dados externos são associados. Consultas à Google Books API funcionam sem chave; `GOOGLE_BOOKS_API_KEY` pode ser configurada opcionalmente para usar uma chave separada, habilitada para a Google Books API, caso queira consultar com uma chave própria.

Para configurar localmente, copie `.env.example` para `.env` e preencha `GOOGLE_DRIVE_API_KEY`. Esse arquivo é ignorado pelo Git e não deve ser enviado ao repositório. Depois, instale as dependências e inicie o Flask:

```powershell
python -m pip install -r requirements.txt
python -m flask --app app run --host 127.0.0.1 --port 5000
```

No serviço de hospedagem, cadastre `GOOGLE_DRIVE_API_KEY` e, se necessário, `GOOGLE_DRIVE_FOLDER_ID` na configuração de ambiente/secrets do serviço em vez de gravá-las no código. Não cadastre nem publique a chave no Git, na página do site ou nos logs.

## Sincronização local com o Google Drive

O sincronizador `drive_sync.py` monitora a pasta `C:\Users\User\Desktop\M4_Books_push\Books`. A cada minuto, envia arquivos novos ao Drive, incluindo PDFs e EPUBs; coloque-os diretamente em `Books`, pois o site cataloga somente EPUBs diretamente dentro da pasta compartilhada. Os metadados não são sincronizados por arquivos: o próprio site consulta Google Books e, se necessário, Open Library. Arquivos iguais são ignorados, livros com o mesmo nome mas conteúdo diferente não sobrescrevem o Drive, e nenhum arquivo remoto é excluído.

O log local do sincronizador fica em `C:\Users\User\Desktop\M4_Books_push\drive_sync.log`.

O envio exige OAuth de uma conta com permissão de Editor na pasta compartilhada. A chave de API usada pelo site é somente para leitura e não serve para uploads:

1. No Google Cloud Console, habilite Google Drive API e configure a tela de consentimento OAuth. Para uma conta pessoal, adicione sua conta como usuário de teste enquanto o app estiver em modo de teste.
2. Crie uma credencial OAuth do tipo **Aplicativo para computador** e salve o JSON em `%LOCALAPPDATA%\M4Books\client_secret.json` (normalmente `C:\Users\User\AppData\Local\M4Books\client_secret.json`). Não o envie ao Git ou a conversas. Na primeira execução, o navegador solicitará autorização; o token renovável também fica nessa pasta local, fora do repositório e do diretório de sincronização.
3. Instale as dependências e teste uma sincronização manual:

```powershell
Set-Location "C:\Users\User\Desktop\M4 Books"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r sync-requirements.txt
.\.venv\Scripts\python.exe .\drive_sync.py --once
```

4. Depois de autorizar e confirmar a primeira execução, registre a tarefa do Windows para iniciar o monitoramento ao entrar na conta:

```powershell
.\scripts\register_drive_sync_task.ps1
```

Para remover a tarefa, execute `.\scripts\register_drive_sync_task.ps1 -Unregister`. O monitoramento só funciona enquanto este computador está ligado e conectado à internet. Em modo de teste OAuth, o Google pode expirar tokens após sete dias; nesse caso, remova `%LOCALAPPDATA%\M4Books\drive_sync_token.json` e faça a autorização novamente. A tarefa não propaga exclusões e não sobrescreve livros diferentes que já tenham o mesmo nome no Drive; esses conflitos aparecem no log.

## Metadados bibliográficos

O título e, quando possível, o autor são inferidos do nome do EPUB no Drive; números de coleção e observações entre parênteses são removidos da busca. Se o nome incluir ISBN, ele é priorizado na pesquisa. Google Books é consultado primeiro; quando não há correspondência confiável, Open Library é consultado como reserva. A ficha usa título, autores, categorias, ano, editora, idioma, ISBN, sinopse, contagem de páginas, capa remota e link da fonte, conforme disponíveis no registro encontrado.

Os metadados bibliográficos e as capas podem corresponder a outra edição ou faltar na API; confira os dados e a fonte antes de tratá-los como correspondência exata do EPUB. Não são consultados nem enviados o conteúdo dos livros.

Os registros encontrados são guardados em `instance/book_metadata.json` por até 30 dias; respostas sem correspondência confiável são guardadas por até seis horas. Em hospedagens com disco efêmero, o cache pode desaparecer após reiniciar o serviço e ser consultado novamente.

## Executar

```powershell
python -m pip install -r requirements.txt
python -m flask --app app run --host 127.0.0.1 --port 5000
```

A página inicial fica em `http://127.0.0.1:5000/`.

## Interface

A recomendação “Um livro para descobrir” avança uma posição no catálogo por dia, usando o calendário de São Paulo. A navegação “Sobre a curadoria” leva à seção de contato, que apresenta somente ícones acessíveis para e-mail e LinkedIn. Os cartões da biblioteca não mostram o selo de metadados pesquisados nem a contagem de resultados.

## Preparação para hospedagem

O `Procfile` inicia o Flask com Waitress, servidor WSGI de produção que funciona em Windows e Linux. Serviços que aceitam Procfile podem usar o comando `web` automaticamente; em outros serviços, configure o comando de inicialização como `waitress-serve --listen=0.0.0.0:$PORT app:app`. O serviço precisa fornecer a variável `PORT`.

Use `python -m pip install -r requirements.txt` como comando de instalação e configure `GOOGLE_DRIVE_API_KEY` nos secrets/variáveis de ambiente da hospedagem. `GOOGLE_BOOKS_API_KEY` é opcional e, se usada, deve ser uma chave própria com acesso à Google Books API.

### Render (teste gratuito)

O arquivo `render.yaml` configura um Web Service Python no plano gratuito, Waitress, verificação de saúde pela rota `/` e a pasta pública do Drive. Ao criar um Blueprint no Render, informe `GOOGLE_DRIVE_API_KEY` quando solicitado; o segredo não está gravado no repositório.

O plano gratuito é apropriado para testes: a instância suspende após 15 minutos sem tráfego e pode levar cerca de um minuto para voltar a responder. O cache de metadados é uma otimização; se arquivos gravados em disco não persistirem após reinicialização, as consultas serão repetidas. Consulte as cotas de horas de instância, tráfego de saída e APIs na conta Render e Google Cloud; aplicações gratuitas não são recomendadas pelo Render para produção.

No Windows local, o servidor WSGI pode ser iniciado sem uma variável `PORT` com:

```powershell
waitress-serve --listen=127.0.0.1:5000 app:app
```

Para escolher a versão Python e configurar os campos específicos (como comando de build, health check e secrets), confirme o provedor antes da publicação. O modo de depuração fica desativado por padrão; só habilite `FLASK_DEBUG=1` em um ambiente local confiável.
