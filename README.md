# M4 Books

Catálogo Flask alimentado pelos nomes de arquivos `.epub` em uma pasta pública do Google Drive. O site lê somente a listagem da pasta; não baixa nem abre o conteúdo dos EPUBs para montar o catálogo. Os metadados pesquisados localmente são associados aos livros pelo `slug` e complementam o catálogo; para itens sem registro local, Open Library e Google Books continuam como fontes automáticas alternativas.

## Configurar o Google Drive

1. Na pasta do Drive, abra **Compartilhar** e defina **Acesso geral → Qualquer pessoa com o link → Leitor**.
2. No Google Cloud Console, selecione ou crie um projeto, ative a **Google Drive API** e crie uma chave de API.
3. Restrinja a chave à Google Drive API. Não publique a chave em repositório nem em páginas do site.
4. Configure `GOOGLE_DRIVE_API_KEY` no ambiente do processo Flask. `GOOGLE_DRIVE_FOLDER_ID` é opcional e, por padrão, usa a pasta informada para este projeto.
5. Coloque os arquivos EPUB diretamente na pasta compartilhada. O catálogo consulta novamente a pasta a cada cinco minutos.

A chave é usada para listar arquivos e nomes pela Drive API. Os downloads redirecionam o navegador ao link público do arquivo no Google Drive, sem passar o conteúdo pelo servidor do site. A pasta e os arquivos precisam permitir leitura a qualquer pessoa com o link. As consultas externas são limitadas e o cache local fica em `instance/openlibrary_metadata.json`. A primeira leitura da pasta acontece em segundo plano; os títulos aparecem enquanto os dados externos são associados.

Para configurar localmente, copie `.env.example` para `.env` e preencha `GOOGLE_DRIVE_API_KEY`. Esse arquivo é ignorado pelo Git e não deve ser enviado ao repositório. Depois, instale as dependências e inicie o Flask:

```powershell
python -m pip install -r requirements.txt
python -m flask --app app run --host 127.0.0.1 --port 5000
```

No serviço de hospedagem, cadastre `GOOGLE_DRIVE_API_KEY` e, se necessário, `GOOGLE_DRIVE_FOLDER_ID` na configuração de ambiente/secrets do serviço em vez de gravá-las no código. Não cadastre nem publique a chave no Git, na página do site ou nos logs.

## Metadados e capas

O arquivo `data/catalogo_metadados.csv` contém os dados pesquisados para os 299 livros atuais, associados pelo `slug` do catálogo. As 257 imagens de capa estão em `static/capas/`; mantenha os dois recursos junto com o aplicativo ao executar ou publicar o site. Os registros locais são usados antes das consultas bibliográficas automáticas. Quando um livro não tem correspondência local, o título e, quando possível, o autor são inferidos do nome do arquivo; números de coleção e observações entre parênteses são removidos da busca. Se o nome incluir ISBN, ele é priorizado na busca automática.

As fichas incluem, quando disponíveis, título pesquisado, autor, categoria, ano, editora, idioma, ISBN, sinopse, número de páginas, encadernação, tradutor, título original, fonte e capa. A página sinaliza o status da pesquisa e deixa visível a fonte consultada. Os dados de publicação, ISBN e capa podem se referir a uma edição diferente do EPUB; confira-os antes de publicar ou associar os dados permanentemente. Gêneros sugeridos são identificados separadamente dos encontrados em fontes.

Para substituir o conjunto local, atualize `data/catalogo_metadados.csv` e os arquivos referenciados na coluna `arquivo_capa`. O aplicativo valida a presença das capas referenciadas ao iniciar.

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

Use `python -m pip install -r requirements.txt` como comando de instalação. Configure `GOOGLE_DRIVE_API_KEY` nos secrets/variáveis de ambiente da hospedagem e mantenha `data/catalogo_metadados.csv` e a pasta `static/capas/` incluídos junto com o aplicativo. Os arquivos de dados e as capas já estão armazenados no projeto e não dependem do diretório local de pesquisa original.

### Render (teste gratuito)

O arquivo `render.yaml` configura um Web Service Python no plano gratuito, Waitress, verificação de saúde pela rota `/` e a pasta pública do Drive. Ao criar um Blueprint no Render, informe `GOOGLE_DRIVE_API_KEY` quando solicitado; o segredo não está gravado no repositório.

O plano gratuito é apropriado para testes: a instância suspende após 15 minutos sem tráfego e pode levar cerca de um minuto para voltar a responder. Arquivos gravados em disco pela aplicação não persistem entre reinicializações, por isso o catálogo pesquisado e as capas são incluídos no próprio projeto. Consulte as cotas de horas de instância e de tráfego de saída na conta Render; aplicações gratuitas não são recomendadas pelo Render para produção.

No Windows local, o servidor WSGI pode ser iniciado sem uma variável `PORT` com:

```powershell
waitress-serve --listen=127.0.0.1:5000 app:app
```

Para escolher a versão Python e configurar os campos específicos (como comando de build, health check e secrets), confirme o provedor antes da publicação. O modo de depuração fica desativado por padrão; só habilite `FLASK_DEBUG=1` em um ambiente local confiável.
