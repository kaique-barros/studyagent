# studyagent (core)

Daemon local que é a **fonte de verdade das tarefas**. Roda como um usuário de sistema próprio, expõe uma API em
**socket Unix** e valida tudo antes de alterar. O Kanban do Obsidian (`Tarefas.md`) passa a ser uma *projeção* gerada.

Esta é a fase 1 do plano (`agente-estudos-design.md`): tarefas, propostas, eventos, projeção, CLI. **Ainda não tem**
Moodle, planejador, materiais/RAG, FSRS nem a interface no Quickshell.

## Conceitos

- **Escopos de token** (um por módulo, com o mínimo de permissão):

  | escopo | pode |
  |---|---|
  | `user` | criar tarefas `manual/study/review`, **concluir**, pedir mudança de data/descarte, **decidir propostas**, ler eventos |
  | `moodle` | criar/atualizar tarefas `source=moodle` (idempotente por `source_ref`) |
  | `planner`, `schedule` | **só propor** (`/proposals`) |
  | `viewer` | só ler |

- **Propostas:** mudar data e descartar tarefa sempre viram proposta pendente; só `user` aprova/rejeita. Se a tarefa
  mudou depois da proposta, ela vira `stale` e não é aplicada.
- **Prazo do Moodle é do Moodle:** ninguém muda o `due` de tarefa `source=moodle` (só `planned_for`).
- **Eventos** (`task.created/updated/completed/dropped`, `proposal.*`) ficam num log append-only. O evento
  `task.completed` traz `day` (o dia vira às 04:00) e `late`, e é o que o XP vai consumir.
- Concluir é idempotente: repetir não gera outro evento (nem XP).

## Instalação (Arch)

Resumo: o daemon roda como o usuário `studyagent` (systemd); você e o Quickshell falam com ele pelo comando `studyagent`, via socket Unix.

### 1. Programa e comando

```bash
sudo install -d /opt/studyagent && sudo python -m venv /opt/studyagent/venv
sudo /opt/studyagent/venv/bin/pip install -r /caminho/para/studyagent/requirements.txt
sudo /opt/studyagent/venv/bin/pip install --no-deps /caminho/para/studyagent
sudo ln -sf /opt/studyagent/venv/bin/studyagent /usr/local/bin/studyagent    # comando global (o Quickshell usa este)
```

### 2. Usuário, pasta da projeção e serviço

```bash
sudo install -m644 deploy/studyagent.sysusers /etc/sysusers.d/studyagent.conf && sudo systemd-sysusers
sudo usermod -aG studyagent $USER            # acesso ao socket (faça logout/login)

# pasta da projeção: só o agente escreve, você lê (assim você não edita o Tarefas.md à mão)
V="$HOME/Obsidian Vault"
sudo setfacl -m u:studyagent:x "$HOME" "$V"
sudo install -d -o studyagent -g studyagent -m 755 "$V/Agente"

sudo install -m644 deploy/studyagentd.service /etc/systemd/system/studyagentd.service
sudo systemctl daemon-reload && sudo systemctl enable --now studyagentd
```

Edite `Environment=STUDYAGENT_TASKS_FILE` e `ReadWritePaths` no `.service` se seu vault não for `~/Obsidian Vault`. O banco fica em `/var/lib/studyagent` (só o agente lê) e o socket em `/run/studyagent/api.sock`.

### 3. Tokens (rode como o usuário do agente; o token só aparece uma vez)

```bash
sa() { sudo -u studyagent env STUDYAGENT_STATE_DIR=/var/lib/studyagent /opt/studyagent/venv/bin/studyagent "$@"; }   # função (vale em bash e zsh)
sa admin init
sa admin token-create shell user --replace --out /tmp/shell.token   # usado por você e pelo Quickshell
mkdir -p ~/.config/studyagent && sudo install -m600 -o $USER /tmp/shell.token ~/.config/studyagent/token && sudo shred -u /tmp/shell.token
```

Os tokens `planner` (agente LLM) e `schedule` (leitor de PDFs) são criados do mesmo jeito; veja as seções abaixo.

### 4. Conferir

```bash
systemctl status studyagentd      # active (running)
studyagent status                 # resumo do dia
studyagent ui snapshot | head -c 300
```

### Atualizar

```bash
sudo /opt/studyagent/venv/bin/pip install --no-deps --upgrade /caminho/para/studyagent
sudo systemctl restart studyagentd            # aplica migrações do banco e novas permissões
systemctl --user restart studyagent-schedule  # se estiver usando o leitor de cronogramas
```

Sempre reinicie o daemon depois de atualizar: um daemon antigo responde 404/403 nos endpoints novos.

### Iniciar / parar

- Daemon: `systemctl start|stop|restart studyagentd` (sobe no boot por causa do `enable`). Logs: `journalctl -u studyagentd -f`.
- Agente LLM e revisão semanal (opcionais): timers em `deploy/` (`studyagent-agent.timer`, `studyagent-review.timer`).
- Leitor de cronogramas (opcional): `systemctl --user enable --now studyagent-schedule.service`.
- Sem systemd (teste): `STUDYAGENT_STATE_DIR=~/.local/state/studyagent studyagent serve`.
- Quickshell: não precisa de nada além do comando `studyagent` e do token em `~/.config/studyagent/token`; a tela de produtividade chama `studyagent ui snapshot`.

## Uso (CLI)

```bash
studyagent status
studyagent task add "Resolver lista 3" --due 2026-11-25 --tag estudo --minutes 40
studyagent task list
studyagent task done 1
studyagent task move 2 --plan 2026-11-26 --reason "prova dia 27"   # vira proposta
studyagent proposal list
studyagent proposal approve 1
studyagent events -f                                                # acompanha em tempo real (SSE)

studyagent dayoff add 2026-11-29       # folga: meta do dia cai de 4 para 2 tarefas
studyagent dayoff list | remove 2026-11-29
studyagent plan show                   # o que o planejador faria agora (não grava)
studyagent plan run                    # gera propostas do plano; aprove com `proposal approve`
```

## Planejador (determinístico, sem LLM)

- Meta: 4 tarefas/dia (2 em dia de folga); horizonte de 14 dias; capacidade contada **por tarefa** (sem `est_minutes`).
- Tarefas com prazo vão para um dia ≤ prazo (prazo mais próximo primeiro); atrasadas pegam os primeiros espaços;
  o que já estava planejado e continua válido não se mexe; o resto preenche as sobras.
- O plano é sempre uma **proposta** `plan.apply` (lote de movimentos); só você aprova. Movimentos de tarefas que
  mudaram desde a proposta são pulados; se todos mudaram, a proposta vira `stale`.
- Roda no primeiro boot/checagem do dia, ao chegar tarefa com prazo em até 3 dias, e quando as folgas mudam.
  Um plano novo substitui o pendente anterior. `STUDYAGENT_PLANNER=0` desliga.
- Folgas: você escolhe as datas; **máx. 10 e mín. 4 por mês**. Só podem ser criadas/removidas a partir de amanhã
  (1 dia de antecedência); remover não pode deixar o mês abaixo de 4. Se algum mês do horizonte tem menos de 4,
  o planejador propõe (`dayoff.add`) domingos, depois sábados, depois dias do fim do mês.
- `summary` mostra `programmed` (tarefas abertas) e `programmed_goal` (24).

## API (socket Unix, `Authorization: Bearer <token>`)

```
GET  /health  /me  /summary
GET  /tasks?status=&from=&to=         POST /tasks          (201 cria, 200 atualiza por source_ref)
POST /tasks/{id}/complete             POST /tasks/{id}/reschedule | /drop     (202, geram proposta)
GET  /proposals?status=               POST /proposals      POST /proposals/{id}/approve | /reject
POST /feedback  (escopo planner)
GET  /courses  /calendar?from=&to=&course=     POST /schedule/shift
GET  /dayoffs  POST /dayoffs {date}  DELETE /dayoffs/{date}
GET  /planner/preview                 POST /planner/run
GET  /events?after=&limit=            GET  /events/stream?after=   (SSE, retoma por Last-Event-ID)
```

Exemplo: `curl --unix-socket /run/studyagent/api.sock -H "Authorization: Bearer $(cat ~/.config/studyagent/token)" http://x/summary`

## Agente com LLM (Groq)

O agente é um **cliente separado** do daemon (o daemon nem tem acesso à internet: `RestrictAddressFamilies=AF_UNIX`).
Ele usa o token `planner`, que só lê e **propõe**; nada muda até você aprovar.

```bash
export GROQ_API_KEY=gsk_...                # opcional: STUDYAGENT_GROQ_MODEL (padrão llama-3.3-70b-versatile)
studyagent agent evaluate [--force]        # avalia o cronograma: propostas (reschedule/drop) + mensagem
studyagent agent review [--days 7]         # feedback da semana (sem chave, sai só com os números)
```

- Avaliação: no máximo 1x por dia; até 5 propostas, só `planned_for` dentro do horizonte e nunca depois do prazo, ou descartar.
  Tudo que o modelo devolve é validado em código; títulos de tarefas são tratados como dados (injeção de prompt só gera
  uma proposta que você pode rejeitar).
- Orçamento do Groq contado localmente (800 req/dia, 150K tokens/dia, prompts ≤ ~7K tokens) em `groq-budget.json`.
- O texto vai para os eventos (`agent.feedback`, `kind` = `evaluation`|`review`) para o shell mostrar.
- Timers: `deploy/studyagent-agent.{service,timer}` (04:30 + 3 min após o boot) e `studyagent-review.*` (domingo 20:00).
  Não testados em systemd real.

## Cronogramas de aula (PDF)

Pasta esperada: `<raiz>/<AAAA-S>/Matérias/<Sigla>/Cronograma.pdf` (padrão da raiz: `~/Obsidian Vault/UFMG`, ou
`STUDYAGENT_SCHEDULE_ROOT`). O semestre e a sigla vêm do caminho. Precisa de um token de escopo `schedule`.

```bash
studyagent schedule scan                  # lê os PDFs novos/alterados (hash) e cria propostas schedule.import
studyagent schedule watch                 # o mesmo, em loop (a cada 5 min); veja deploy/studyagent-schedule.service
studyagent schedule import caminho.pdf    # um arquivo só
studyagent proposal list                  # mostra o resumo e os avisos de cada cronograma
studyagent proposal approve N             # só então viram matéria, calendário e tarefas
studyagent schedule courses | calendar [--from --to --course]
studyagent schedule shift 2027-1 --start 2026-12-05 [--k 1] [--course SIGLA] | --clear
```

- **Leitura:** as tabelas do PDF (pdfplumber) são lidas pelo cabeçalho (Aula, Data, Dia, Horário, Assunto/Conteúdo);
  funciona para o Diário de Classe da UFMG e para tabelas de professores (datas sem ano usam o semestre da pasta; o
  horário pode vir do cabeçalho do documento). Sem nenhuma tabela, e com `GROQ_API_KEY`, o texto vai ao Groq em pedaços e a
  resposta é validada. Conferências: dia da semana × data, sequência das aulas, janela do semestre.
- **Calendário** (`session`): cada linha vira `class`, `exam`, `holiday`, `no_class` ou `empty` (rótulo "SIGLA: vazio"
  ou o texto do PDF). Aulas de revisão/dúvidas ficam com `flag` e `highlight` (junto com provas, feriados e dias sem aula).
- **Tarefas de estudo:** toda aula (`class`) vira uma tarefa `study` (`source=schedule`, `course=SIGLA`) com **prazo
  fixo = data da aula − 5 dias** (atrasar tem a penalidade normal). Provas, feriados e linhas vazias não geram tarefa.
  A avaliação (pontos, testes do Moodle, VPLs, checkpoints) é ignorada de propósito: vem do Moodle ou é manual.
- **Adaptar datas** (`schedule shift`): a 1ª aula de cada matéria passa a ser estudada em `--start` e as demais mantêm o
  espaçamento × `k` (0,5 = comprime à metade). Muda só os prazos das tarefas abertas; as datas do calendário não mudam.
- **Reimportar** um PDF alterado atualiza prazos e títulos, remove aulas que sumiram (a tarefa aberta é descartada) e
  nunca mexe em tarefas concluídas.

## Desenvolvimento

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pip install -e . --no-deps && pytest
STUDYAGENT_STATE_DIR=./state STUDYAGENT_SOCKET=/tmp/sa.sock studyagent admin init && studyagent serve
```

## Limitações conhecidas

- O `studyagentd.service` (endurecimento, `ReadWritePaths` com espaço no caminho, `MemoryDenyWriteExecute`) **não foi
  testado em uma máquina real**; rode `systemd-analyze verify` e, se o serviço não subir, afrouxe as opções de
  endurecimento uma a uma.
- Tarefa "planejada" (`planned_for`) que não foi feita no dia ainda não conta como atrasada: `late` compara só com `due`.
  Streak/mínimo diário ainda não são calculados no core (o XP do Quickshell os consome dos eventos).
- `late` e `day` já vêm no evento, mas o XP do Quickshell ainda lê o `[x]` do arquivo (será trocado pelo SSE).

## Progresso e tela de produtividade

- `studyagent ui snapshot` / `ui progress`: JSON para o Quickshell (sem HTTP no QML). `studyagent task start|pause|done|move`.
- XP, nível, streak, freezes: `progress.py` (função pura), calculado do log de eventos + `day_snap` (foto diária; o dia vira às 04:00).
- Premissas ajustáveis: bônus de dia cumprido 20 XP, freeze a cada 60 tarefas (máx. 3), semana = 7 dias corridos, fita de entrega = tarefa que não é `study` e tem prazo.
- O comando global `studyagent` é instalado na seção Instalação.
