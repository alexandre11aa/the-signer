# Assinador de PDF (multi-signature)

Aplicação web para assinar digitalmente PDFs com um certificado `.pfx`/`.p12`, com assinatura **visível**, uma por página, em um de três estilos: padrão, Adobe Acrobat ou personalizado (com a sua própria imagem). O servidor é **stateless**: processa tudo em memória e não guarda nada.

## Sumário

1. [Propósito](#propósito)
2. [Como funciona](#como-funciona)
3. [Arquitetura](#arquitetura)
4. [Tecnologias](#tecnologias)
5. [Segurança](#segurança)
6. [Limitações e o que você precisa saber](#limitações-e-o-que-você-precisa-saber)
7. [API](#api)
8. [Como executar](#como-executar)
9. [Colocando em produção](#colocando-em-produção)
10. [Configuração](#configuração)

---

## Propósito

Assinar um PDF com certificado digital normalmente exige o Adobe Acrobat ou ferramentas pesadas. Este projeto oferece um fluxo simples:

1. escolher o PDF e o certificado `.pfx`;
2. informar a senha;
3. indicar **quais páginas** assinar (ex.: `1,3,5-8` ou `all`);
4. escolher **onde** a assinatura aparece na página (grade 3×3);
5. escolher o **estilo** (padrão, Adobe ou personalizado; neste último você envia um PNG de 264×264 px que ocupa o lugar do símbolo da Adobe);
6. baixar o PDF assinado.

A prioridade do projeto é **não reter nada**: nem o PDF, nem o certificado, nem a senha, nem em disco, nem em banco, nem em logs.

## Como funciona

- Cada página selecionada recebe **uma assinatura própria**, aplicada como **atualização incremental** do PDF. Assim, uma assinatura não invalida a anterior e o documento final contém N assinaturas encadeadas.
- A posição respeita a **rotação da página** (`/Rotate`), então a assinatura aparece onde o usuário espera mesmo em páginas giradas.
- O certificado é aberto em memória a cada requisição e descartado ao final.

## Arquitetura

```
┌───────────────────────────┐        HTTPS         ┌──────────────────────────────────────┐
│ Navegador                 │  JSON (base64)       │ Container Docker                     │
│  HTML + CSS + JS puro     │ ───────────────────► │  FastAPI + Uvicorn                   │
│  - pré-visualiza o PDF    │                      │   ├─ lê o corpo só em memória        │
│    localmente (pdf.js)    │ ◄─────────────────── │   ├─ pyHanko assina (BytesIO)        │
│  - POST /api/sign         │  PDF assinado        │   └─ responde e descarta tudo        │
└───────────────────────────┘                      └──────────────────────────────────────┘
                                                    Sem banco · sem volumes · sem logs
```

### Estrutura de pastas

```
multi-signature/
├─ Dockerfile
├─ docker-compose.yml
├─ requirements.txt
├─ README.md
└─ app/
   ├─ core/                # todo o código Python
   │  ├─ main.py           # app FastAPI: rotas, middleware, tratamento de erros
   │  ├─ config.py         # limites, caminhos e constantes
   │  ├─ signer.py         # lógica de assinatura (pyHanko), só em memória
   │  └─ utils.py          # parse de páginas, geometria da caixa, UserInputError
   ├─ static/
   │  ├─ css/style.css
   │  ├─ js/app.js
   │  ├─ js/vendor/       # pdf.js (pré-visualização), hospedado localmente
   │  └─ img/adobe_logo.png
   └─ template/
      └─ index.html
```

### Responsabilidade de cada módulo

| Arquivo | Responsabilidade |
|---|---|
| `core/main.py` | Define as rotas, lê o corpo com limite de tamanho, aplica cabeçalhos de segurança e converte qualquer erro em um código genérico. Serve a página e os estáticos. |
| `core/signer.py` | Carrega o PKCS#12 a partir de bytes, monta a aparência da assinatura (padrão ou Adobe) e assina página por página. |
| `core/utils.py` | Funções puras: interpreta `"1,3,5-8"`, calcula o retângulo da assinatura considerando a rotação e define `UserInputError`. |
| `core/config.py` | Limites (tamanho, páginas, timeout), grade de posições e caminhos. |
| `static/js/app.js` | Pré-visualiza o PDF (pdf.js), mostra onde a assinatura vai ficar, converte os arquivos para base64, chama a API e, depois de assinar, oferece os botões "Baixar PDF" e "Novo PDF". Os avisos aparecem como toasts com mensagens genéricas, já que o servidor não guarda logs. O PDF assinado fica só na memória da aba até o download ou o reinício. Não persiste nada no navegador. |

## Tecnologias

| Camada | Tecnologia | Versão |
|---|---|---|
| Linguagem | Python | 3.12 |
| API | FastAPI | 0.115.6 |
| Servidor ASGI | Uvicorn | 0.34.0 |
| Assinatura digital de PDF | pyHanko | 0.38.1 |
| Leitura de PDF (páginas, rotação) | pypdf | 6.21.4 |
| Imagens (símbolo dos estilos Adobe e personalizado) | Pillow | 13.0.0 |
| Frontend | HTML, CSS e JavaScript puros | sem framework |
| Pré-visualização do PDF | [pdf.js](https://github.com/mozilla/pdf.js) (Mozilla, Apache-2.0) | 3.11.174, hospedado em `static/js/vendor/`, sem CDN em tempo de execução |
| Empacotamento | Docker e Docker Compose | |

As versões estão fixadas em `requirements.txt` para builds reproduzíveis.

## Segurança

O objetivo é que o servidor **não retenha nada**. As medidas abaixo estão implementadas e foram verificadas em teste.

### Nada é gravado

| Medida | Onde |
|---|---|
| Corpo da requisição lido **apenas em memória**, abortando assim que passa do limite | `core/main.py` (`read_limited_body`) |
| Envio em **JSON com base64**, em vez de multipart (que grava em disco temporário acima de 1 MB) | API e `app.js` |
| PDF e certificado manipulados só com `BytesIO` | `core/signer.py` |
| Sistema de arquivos do container **somente leitura** | `docker-compose.yml` (`read_only: true`) |
| `/tmp` é um **tmpfs em RAM** (16 MB, `noexec`, `nosuid`) | `docker-compose.yml` |
| Sem volumes e sem banco de dados | `docker-compose.yml` |
| **Core dumps desativados**, para a memória não ser despejada em arquivo | `ulimits: core: 0` |

Verificado: `docker diff` não mostra nenhuma alteração no container após assinar, e gravar em `/srv` falha com "Read-only file system".

### Nada vai para os logs

- Uvicorn roda com `--no-access-log` e `--log-level warning`.
- O driver de log do container é `none`.
- Os erros devolvem **somente um código fixo** (ex.: `{"error": "invalid_certificate_or_password"}`), nunca a mensagem da exceção, o traceback ou o conteúdo enviado.
- O handler padrão de erro de validação do FastAPI foi substituído, porque ele devolve o valor enviado (incluindo a senha) na resposta 422.
- Se o certificado não abre, a exceção original é descartada, pois pode conter detalhes do material criptográfico.
- Erros de biblioteca durante a assinatura viram um único código (`signing_failed`); o texto original nunca é exposto.
- Um middleware captura qualquer exceção inesperada e responde `internal_error` **sem relançá-la**. Antes, o Starlette relançava a exceção depois de responder e o Uvicorn imprimia o traceback completo.

### Endurecimento do container

- Usuário sem privilégios (UID 10001).
- `cap_drop: ALL` (nenhuma capability do Linux).
- `no-new-privileges: true`.
- Limites de recursos: 512 MB de memória e 128 processos.
- Porta publicada só em `127.0.0.1`.
- `.dockerignore` impede `.pfx`, `.p12`, `.pdf` e `.env` de entrarem na imagem.

### Proteções da API e do navegador

- **Limites:** corpo de 40 MB, PDF de 25 MB, certificado de 256 KB, 100 páginas por operação, timeout de 60 s.
- **Sem documentação exposta:** `/docs`, `/redoc` e `/openapi.json` estão desativados.
- **Cabeçalhos em todas as respostas:**
  - `Content-Security-Policy` restritiva (`default-src 'none'`; scripts, estilos e conexões só da própria origem; sem `frame-ancestors`);
  - `Cache-Control: no-store`;
  - `X-Content-Type-Options: nosniff`;
  - `Referrer-Policy: no-referrer`;
  - `Strict-Transport-Security`, que só tem efeito quando servido por HTTPS.
- **Mesma origem:** frontend e API saem do mesmo servidor, então não há CORS aberto.
- **Pré-visualização local:** o PDF é lido e desenhado no navegador pelo pdf.js. Ele só é enviado ao servidor quando você clica em "Assinar".
- **Frontend:** o único código de terceiros é o pdf.js, servido pela própria aplicação (sem CDN). Não há analytics, `localStorage` nem cookies; `autocomplete="off"` na senha, e o campo é limpo após cada envio. Requisições com `credentials: "omit"` e `referrerPolicy: "no-referrer"`.
- **Senha:** enviada no corpo (nunca em URL ou cabeçalho).

## Limitações e o que você precisa saber

Este projeto reduz bastante o risco, mas **não o elimina**. Leia antes de usar com terceiros.

1. **O servidor vê a chave privada e a senha durante a requisição.** "Stateless" é um comportamento do servidor, não uma garantia técnica para quem usa. Quem opera o servidor (ou invade) pode ver esses dados em memória, ou alterar o código para capturá-los. Por isso o uso é adequado para **você ou uma equipe que confia no operador**. Para terceiros, uma aplicação desktop ou local é mais segura, pois o usuário pode auditar e bloquear a rede.
2. **Certificados pessoais (e-CPF, ICP-Brasil):** os termos de titularidade geralmente proíbem compartilhar a chave ou a senha. Enviar a um servidor de terceiros pode violá-los.
3. **Python não garante zerar a memória.** Senha e chave podem permanecer em memória até o coletor de lixo liberar. Se o host tiver swap, a memória pode ir para disco. Desative o swap ou use swap criptografado.
4. **Sem HTTPS embutido.** A aplicação escuta em HTTP, em `127.0.0.1`. Para acesso pela rede é obrigatório um proxy com TLS (veja abaixo).
5. **Sem autenticação e sem rate limit.** Se publicado, qualquer pessoa pode usar o servidor como serviço de assinatura e consumir CPU. Adicione ao menos rate limit e, de preferência, login.
6. **O timeout não mata a thread.** Os 60 s liberam a resposta, mas a assinatura em andamento continua até terminar.
7. **A cadeia do certificado não é validada.** O app assina com o certificado que receber, inclusive autoassinado. A integridade criptográfica é verificada, mas a confiança na cadeia depende do leitor de PDF.
8. **PDFs criptografados não são suportados.** Remova a proteção antes.
9. **PDFs com xref híbrido** (tabela clássica + `/XRefStm`, comum em arquivos gerados por algumas ferramentas) são recusados pelo pyHanko, porque não podem ser estendidos com segurança. Quando o PDF ainda **não tem assinaturas**, ele é reescrito uma vez com o pypdf (o conteúdo é preservado) e depois assinado. Se for híbrido **e já assinado**, é recusado (`unsupported_pdf`), pois reescrevê-lo invalidaria as assinaturas existentes. Assinar em modo não estrito foi descartado: nos testes gerou arquivos inconsistentes.
10. **Proxies, CDNs e WAFs podem registrar o corpo das requisições.** Se você colocar algo na frente, confirme que não registra o corpo.
11. **A aparência do carimbo não foi conferida no Acrobat.** A API, a validade criptográfica e a interface (desktop e celular, no Edge) foram testadas, mas abrir o PDF assinado no Acrobat continua sendo uma verificação sua.

## API

Todas as respostas de erro têm o formato `{"error": "<código>"}`.

### `GET /health`
Retorna `{"status": "ok"}`.

### `POST /api/sign`
Assina e devolve o PDF (`application/pdf`).

```json
{
  "pdf_b64": "<PDF em base64>",
  "pfx_b64": "<certificado em base64>",
  "password": "senha do certificado",
  "pages": "1,3,5-8",
  "position": 9,
  "style": "adobe",
  "logo_b64": "<PNG 264x264 em base64; só para style \"custom\">"
}
```

| Campo | Valores |
|---|---|
| `pages` | `"all"`, `"*"` ou lista/intervalos como `"1,3,5-8"` (páginas contadas a partir de 1) |
| `position` | `1` a `9`, grade 3×3: `1` canto superior esquerdo … `5` centro … `9` canto inferior direito |
| `style` | `"default"`, `"adobe"` ou `"custom"` |
| `logo_b64` | Obrigatório com `style: "custom"`, ignorado nos outros. Deve ser um **PNG de exatamente 264×264 px** (até 512 KB). O formato e as dimensões são verificados no servidor pelo cabeçalho do arquivo, antes de decodificar os pixels, e a imagem existe só em memória. No estilo personalizado a imagem ocupa o mesmo lugar do símbolo da Adobe, com o mesmo layout. |

### Códigos de erro

| Código | Significado |
|---|---|
| `invalid_certificate_or_password` | Certificado inválido ou senha incorreta |
| `invalid_pdf` | Não foi possível ler o PDF |
| `encrypted_pdf` | PDF protegido |
| `invalid_pages` | Formato de páginas inválido |
| `pages_out_of_range` | Página fora do intervalo do documento |
| `too_many_pages` | Mais de 100 páginas na operação |
| `payload_too_large` | Corpo acima do limite (HTTP 413) |
| `invalid_file_size` | Arquivo vazio ou maior que o limite |
| `invalid_base64` | Base64 inválido |
| `invalid_style` | Estilo desconhecido |
| `invalid_logo` | Imagem personalizada ausente, não PNG ou fora de 264×264 px |
| `invalid_request` | Corpo malformado ou campos faltando (HTTP 422) |
| `unsupported_pdf` | PDF com estrutura não suportada (ex.: híbrido que já está assinado) |
| `signing_failed` | Falha ao assinar; sem detalhes, de propósito |
| `timeout` | Operação excedeu 60 s |
| `internal_error` | Erro inesperado (HTTP 500) |

## Como executar

### Com Docker (recomendado)

```bash
docker compose up -d --build
```

Abra <http://127.0.0.1:8000>. Para parar:

```bash
docker compose down
```

Como o driver de log é `none`, `docker logs` não mostra nada. Isso é intencional. Atenção: o driver só impede a leitura via `docker logs`; se você rodar `docker compose up` **sem `-d`**, a saída do Uvicorn aparece no seu terminal (avisos e erros de nível `warning`, sem tracebacks de erros inesperados e sem corpo de requisição).

### Sem Docker (desenvolvimento)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn app.core.main:app --port 8000 --no-access-log --log-level warning
```

## Colocando em produção

A aplicação não termina TLS. Coloque um proxy reverso na frente, com **log de acesso desligado** e limite de corpo. Exemplo com Caddy (confira a documentação da sua versão):

```
assinador.example.com {
    request_body {
        max_size 40MB
    }
    reverse_proxy 127.0.0.1:8000
}
```

Checklist:

- [ ] HTTPS ativo (o HSTS só vale sob HTTPS).
- [ ] Proxy, CDN e WAF **sem registrar o corpo** das requisições.
- [ ] Swap desativado no host, ou criptografado.
- [ ] Rate limit e, se público, autenticação.
- [ ] Dependências atualizadas (pypdf e pyHanko já tiveram vulnerabilidades de parsing).
- [ ] Hospedagem sob seu controle, sem plataforma que capture logs automaticamente.

## Configuração

Os limites ficam em `app/core/config.py`:

| Constante | Padrão | Descrição |
|---|---|---|
| `MAX_BODY_BYTES` | 40 MB | Corpo total da requisição (base64 ocupa ~1,37× o arquivo) |
| `MAX_PDF_BYTES` | 25 MB | PDF decodificado |
| `MAX_PFX_BYTES` | 256 KB | Certificado decodificado |
| `MAX_LOGO_BYTES` | 512 KB | Imagem do estilo personalizado, decodificada |
| `CUSTOM_LOGO_PIXELS` | 264 × 264 | Tamanho exato exigido para a imagem personalizada |
| `MAX_PAGES_TO_SIGN` | 100 | Páginas por operação |
| `SIGN_TIMEOUT_SECONDS` | 60 | Tempo máximo de assinatura |
| `STAMP_WIDTH`, `STAMP_HEIGHT`, `STAMP_MARGIN` | 210, 60, 20 | Geometria da assinatura no estilo padrão (pontos PDF) |

O app web **não usa `.env`**: certificado e senha são informados pelo usuário a cada assinatura.

