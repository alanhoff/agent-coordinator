<h1 align="center">Agent Coordinator</h1>
<p align="center">
  <a href="README.md">English</a>
  ·
  <a href="README.pt-BR.md">Português (Brasil)</a>
  ·
  <a href="README.es.md">Español</a>
  ·
  <a href="README.zh-CN.md">简体中文</a>
</p>
<p align="center"><strong>Dê ao Codex um trabalho complexo. Receba um plano claro e um resultado verificado.</strong></p>
<p align="center">Mantenha trabalhos longos fáceis de entender, acompanhe o progresso sem dificuldade e, após uma interrupção, retome até chegar a um resultado verificado.</p>
<p align="center">
  <img src=".github/readme/agent-coordinator-hero.png" width="880" alt="Ilustração de uma solicitação complexa que percorre vários fluxos de trabalho delimitados, com pontos de verificação, e retorna como um único resultado verificado.">
</p>
<p align="center">
  <a href="#instale-com-um-único-prompt"><strong>Instale com um único prompt</strong></a>
  ·
  <a href="#veja-como-funciona-em-uma-tarefa-do-dia-a-dia">Veja um exemplo do dia a dia</a>
</p>
<p align="center"><sub>Licença MIT · Instalação na sua conta de usuário · Não altera as configurações do Codex</sub></p>

## O que você recebe

- Um plano claro que você pode acompanhar do pedido à conclusão.
- Partes com responsabilidades definidas, cada uma com propósito e responsável claros.
- Um resultado verificado e recuperável, capaz de resistir a interrupções.

## O Agent Coordinator é indicado para você?

| Use quando | Provavelmente não é necessário quando |
|---|---|
| O trabalho tem várias etapas, arquivos ou especialidades interdependentes. | A tarefa é uma única etapa pequena e óbvia. |
| Várias partes independentes podem avançar com segurança. | Uma resposta rápida ou uma alteração mínima é suficiente. |
| Uma interrupção dificultaria reconstruir o progresso. | Seria fácil recomeçar a partir do prompt original. |

## Veja como funciona em uma tarefa do dia a dia

> Adicione buscas salvas ao meu aplicativo sem quebrar o checkout.

1. **Defina claramente o que precisa ser entregue:** identifique o comportamento das buscas salvas, a proteção do checkout e como cada um será verificado.
2. **Mantenha o progresso fácil de entender:** separe a análise, a alteração pontual e o teste de regressão para que cada parte tenha um propósito claro.
3. **Verifique antes de concluir:** analise os arquivos alterados e as evidências de verificação; após uma interrupção, continue a partir do progresso registrado em vez de recomeçar.

A tarefa só termina quando as buscas salvas atendem ao critério de aceitação e as verificações existentes do checkout continuam passando.

## Instale com um único prompt

Peça ao Codex que siga o [instalador mantido no repositório](INSTALL.md):

```text
Instale https://github.com/alanhoff/agent-coordinator seguindo o INSTALL.md
```

O procedimento clona o repositório em um diretório temporário, registra o commit, monta uma skill nova, instala-a para o usuário atual, remove o checkout temporário e informa o caminho de instalação e o commit de origem. Um destino existente só é substituído quando se identifica como Coordinator.

O Coordinator requer Python 3.11 ou mais recente e nenhum pacote de terceiros para execução. A instalação não edita as configurações do Codex nem registra perfis globais de agentes personalizados.

| Local do usuário atual | O que é armazenado ali |
|---|---|
| `~/.agents/skills/coordinator` | A skill, os perfis de função, as referências, os adaptadores Python e o código de runtime incluído |
| `~/.agent-coordinator` | Sessões privadas, bloqueios, dados de recuperação e estado do fluxo de trabalho |

## Experimente uma primeira tarefa

Em um projeto, envie este prompt inicial:

```text
$coordinator Analise o README deste projeto em busca de etapas de configuração confusas. Não edite arquivos.
Retorne as três correções de maior impacto, cite as evidências de cada uma e confirme que nenhum arquivo foi alterado.
```

Uma resposta bem-sucedida atende a três condições:

1. As três correções estão classificadas por impacto.
2. Cada correção cita evidências do projeto.
3. A resposta confirma que nenhum arquivo foi alterado.

## Como funciona

O Coordinator segue as mesmas quatro etapas em cada trabalho:

1. **Entender:** explicitar o resultado solicitado, as restrições e a comprovação de sucesso.
2. **Dividir:** decompor o trabalho nas menores partes úteis, com limites e dependências claros.
3. **Executar:** trabalhar nas partes prontas em uma ordem segura. Agentes especialistas são opcionais; quando não estão disponíveis, o Coordinator executa cada parte diretamente pelo mesmo processo.
4. **Verificar e recuperar:** examinar o resultado e suas evidências, reconciliar trabalhos incertos antes de tentar novamente e concluir somente depois que os requisitos e impedimentos forem resolvidos.

A sequência persistente de comandos transforma essas etapas em operações seguras:

```text
plan-apply → next → node-route-auto → node-claim → node-start → node-complete
           ↘ refine/split/reconcile conforme necessário ↗
                         workflow-complete
```

`next` é somente leitura e informa a próxima classe de ação permitida sem incorporar todo o estado do fluxo de trabalho.

## Perguntas frequentes

<details>
<summary>Isso exige vários agentes?</summary>

Não. Quando há capacidade disponível para agentes adicionais, o Coordinator pode enviar partes independentes a agentes distintos; caso contrário, ele as executa diretamente, uma de cada vez.

</details>

<details>
<summary>Preciso invocá-lo explicitamente?</summary>

Sim. O padrão de prompt documentado inicia cada tarefa coordenada com `$coordinator`; a instalação apenas disponibiliza a skill e não altera nenhuma configuração.

</details>

<details>
<summary>O que ele adiciona ao meu projeto ou às configurações?</summary>

Ele não adiciona ao projeto de destino nenhum arquivo persistente pertencente ao Coordinator e não edita as configurações do Codex nem os perfis globais de agentes personalizados. Durante a inicialização, ele cria e remove um único arquivo com nome reservado, usado exclusivamente para detectar como o sistema de arquivos do repositório diferencia maiúsculas de minúsculas; no Windows, isso é determinado sem um arquivo de teste.

</details>

<details>
<summary>O que acontece se o trabalho for interrompido?</summary>

Uma nova execução do Coordinator pode retomar o trabalho a partir do estado privado. Antes de tentar novamente, ela marca para reconciliação qualquer trabalho que ainda possa estar ativo, preserva as evidências já obtidas e evita iniciar duas vezes uma etapa cujo estado é incerto.

</details>

## Referência

<details>
<summary>Padrões de prompt</summary>

Use o prefixo explícito `$coordinator` e defina o que precisa ser entregue. Estes padrões abrangem implementação, diagnóstico, recuperação e comparação baseada apenas em evidências.

```text
$coordinator Entregue o recurso de buscas salvas. Consulte as instruções do repositório, preserve os critérios
de aceitação, separe apenas o trabalho independente, valide o comportamento integrado e conclua com evidências
concretas e verificáveis.
```

```text
$coordinator Reproduza e diagnostique o teste intermitente do checkout antes de alterar o código de produção.
Mantenha o diagnóstico, a menor correção possível na camada responsável e a validação independente como partes
dependentes.
```

```text
$coordinator Retome o fluxo de trabalho interrompido de migração de esquema. Reconcilie o trabalho incerto antes
de tentar novamente, preserve as evidências já obtidas e valide o comportamento tanto ao reverter quanto ao aplicar a migração.
```

```text
$coordinator Compare as arquiteturas propostas para processamento de eventos com os limites e requisitos atuais
do repositório. Informe as vantagens e desvantagens e as evidências ausentes; depois, recomende uma delas sem
implementar nenhuma das opções.
```

</details>

<details>
<summary>Avaliação e ciclo de vida</summary>

O Coordinator registra cinco dimensões de complexidade de 0 a 4: abrangência, superfície de alteração, acoplamento, novidade e verificação. Separadamente, registra fatores de ambiguidade de 0 a 4 para o objetivo, as entradas, os limites, as dependências e a aceitação.

Os limites padrão incluem o valor indicado: uma complexidade total de 6 ou qualquer dimensão com valor 3 exige divisão, enquanto uma ambiguidade total de 4 ou qualquer fator com valor 2 exige refinamento. A profundidade máxima padrão de refinamento é 8.

```text
assess → refine or split → route → claim → execute → validate → reassess
```

Toda folha avaliável e não bloqueada precisa estar atualizada e ser executável antes do início do roteamento. Requisitos alterados ou resultados efetivos das dependências podem desatualizar trabalhos posteriores; por isso, a verificação de ponto fixo se repete depois de alterações relevantes nas evidências.

</details>

<details>
<summary>Responsabilidade, roteamento e conclusão</summary>

- Cada parte executável tem critérios de aceitação, uma função e zero ou mais `write_scopes` normalizados e relativos ao repositório.
- Escopos vazios representam trabalhos baseados apenas em evidências e exigem `change_surface=0`. Trabalhos que produzem artefatos exigem uma pontuação positiva de superfície de alteração e pelo menos um escopo.
- Trabalhos independentes em execução não podem ter escopos sobrepostos. A comparação entre maiúsculas e minúsculas segue o comportamento detectado no sistema de arquivos de destino.
- O roteamento classifica somente os candidatos disponibilizados pelo runtime ativo. Se não houver um catálogo atual disponível ou se a seleção falhar, a execução herdará o modelo e o nível de esforço do pai.
- A ação de assumir um trabalho registra uma linha de base SHA-256 para cada escopo de artefato. A conclusão exige que cada escopo declarado continue existindo e tenha sido alterado durante aquela tentativa.
- A conclusão do fluxo de trabalho exige requisitos e impedimentos resolvidos, evidências válidas e apenas estados terminais permitidos. O Coordinator não invoca nem inspeciona nenhum sistema de controle de versão.

</details>

<details>
<summary>Inspeção do estado, inclusive no Windows</summary>

Os documentos persistidos dos fluxos de trabalho, no esquema v6, ficam em `~/.agent-coordinator/workflows`. Estes comandos são somente leitura e nunca criam, bloqueiam, reparam, normalizam, armazenam em cache nem fazem limpeza no estado.

```sh
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py list --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py status --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py context --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py next --workflow-id WORKFLOW --json
```

No Prompt de Comando do Windows, use `python` e o caminho do usuário atual:

```bat
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" list --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" status --workflow-id WORKFLOW --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" context --workflow-id WORKFLOW --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" next --workflow-id WORKFLOW --json
```

No Windows, o estado fica em `%USERPROFILE%\.agent-coordinator\workflows`.

</details>

<details>
<summary>Demonstração com Docker</summary>

A demonstração usa uma versão fixada da imagem universal do OpenAI Codex e requer `OPENAI_API_KEY` no arquivo `.env` ignorado na raiz. As montagens da skill e do código-fonte são somente leitura; a saída mutável permanece no diretório ignorado `data/`.

Gere o backend com o Coordinator:

```sh
docker compose run --rm coordinator
```

`data/project/` deve estar limpo; um arquivo regular `.nvmrc` já existente é a única entrada permitida no nível superior. Preserve tudo o que for necessário antes de esvaziar `data/` para outra execução.

O aplicativo gerado fica em `data/project/`, com o backend em `data/project/backend/`. As sessões do Codex, o estado do Coordinator e os dados do SQLite usam diretórios no mesmo nível, e o aplicativo gerado permanece fora da verificação automatizada do repositório.

Inicie o backend gerado manualmente:

```sh
docker compose up backend
```

A API estará disponível em `http://localhost:3000`, e o banco de dados SQLite persistirá em `data/sqlite/todos.db`.

</details>

## Projeto

- [Licença MIT](LICENSE)
- [Guia de contribuição](CONTRIBUTING.md)
- [Política de segurança](SECURITY.md)
- [Repositório no GitHub](https://github.com/alanhoff/agent-coordinator)
- [Rastreador público de issues](https://github.com/alanhoff/agent-coordinator/issues)
