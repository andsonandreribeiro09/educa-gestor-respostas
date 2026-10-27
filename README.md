# Educ@ · Formulário e painel do gestor

Portal único para teste de preenchimento, recebimento de submissões e análise de respostas no Streamlit Community Cloud.

## O que está incluído

- Formulário com os 98 campos do esquema original, organizado em 12 etapas, com condições, anexos, validação de pendências e consulta do ISSN na Crossref.
- Registro de novas submissões em uma base compartilhada pelas sessões do portal. Reenvios acidentais da mesma sessão usam o mesmo protocolo.
- Comprovante PDF e cópia JSON; o teste não envia e-mails nem submete ao Google Forms ou ao Portal Educ@ institucional.
- Área do gestor com código de acesso, os gráficos e dossiês do painel original, além de download dos anexos de novas submissões.
- Cinco revistas de demonstração com contatos e notas internas removidos. Os anexos originais continuam no computador de origem; não estão neste repositório.
- Exportação e restauração de backup dos novos envios com seus anexos.

## Execução local

Python 3.12. Instale `pip install -r requirements.txt` e execute `streamlit run streamlit_app.py`.

Configure o código de acesso em `.streamlit/secrets.toml`, que nunca deve ser versionado:

```toml
manager_password = "seu-codigo-de-acesso"
```

## Deploy no Community Cloud

Selecione o repositório `andsonandreribeiro09/educa-gestor-respostas`, branch `main` e arquivo `streamlit_app.py`. Nas configurações avançadas, use Python 3.12 e defina `manager_password` nos Secrets. O formulário fica acessível pelo link; a área do gestor exige esse código.

- Formulário: URL do app com `?area=formulario`.
- Gestor: URL do app com `?area=gestor`.

## Limites deste teste

O SQLite e os anexos dos novos envios usam o disco temporário do Streamlit. **A plataforma pode apagar esses dados em reinícios, reimplantações ou realocação do app.** Exporte o backup após cada rodada e restaure-o se necessário. A base local das portas 8200/8201 não é sincronizada com este portal.

Para coleta real e contínua, é necessário conectar armazenamento persistente e autenticação individual dos gestores. O código compartilhado de acesso é apenas uma proteção de teste; não substitui uma identidade institucional. A validação de ISSN desta versão consulta a Crossref; periódicos não registrados nessa fonte exigem outra verificação antes de habilitar o envio. As sugestões de preenchimento e a derivação assistida por fontes externas do formulário local não foram portadas para este piloto.

Todos os indicadores permanecem preliminares e não representam decisão de aprovação. O ano corrente é parcial; as instruções originais e os anexos ainda precisam de conciliação da janela anual antes de avaliação institucional.

Fonte do código: formulário e painel locais Educ@. Dados de demonstração preparados em 27/10/2026.
