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

```bash
sudo install -d /opt/studyagent && sudo python -m venv /opt/studyagent/venv
sudo /opt/studyagent/venv/bin/pip install -r /caminho/para/studyagent/requirements.txt
sudo /opt/studyagent/venv/bin/pip install --no-deps /caminho/para/studyagent
sudo install -m644 deploy/studyagent.sysusers /etc/sysusers.d/studyagent.conf && sudo systemd-sysusers
sudo usermod -aG studyagent $USER            # acesso ao socket (faça logout/login)

# pasta da projeção: só o agente escreve, você lê (assim você não edita o Tarefas.md à mão)
V="$HOME/Obsidian Vault"
sudo setfacl -m u:studyagent:x "$HOME" "$V"
sudo install -d -o studyagent -g studyagent -m 755 "$V/Agente"

sudo install -m644 deploy/studyagentd.service /etc/systemd/system/studyagentd.service
sudo systemctl daemon-reload && sudo systemctl enable --now studyagentd
```

Tokens (rode como o usuário do agente; o token só aparece uma vez):

```bash
SA="sudo -u studyagent env STUDYAGENT_STATE_DIR=/var/lib/studyagent /opt/studyagent/venv/bin/studyagent"
$SA admin init
$SA admin token-create shell user --replace --out /tmp/shell.token   # --replace: substitui um token "shell" antigo
mkdir -p ~/.config/studyagent && sudo install -m600 -o $USER /tmp/shell.token ~/.config/studyagent/token && sudo shred -u /tmp/shell.token
```

No Quickshell, aponte o `tasksFile` do `CalendarBox` para `~/Obsidian Vault/Agente/Tarefas.md`.

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
GET  /dayoffs  POST /dayoffs {date}  DELETE /dayoffs/{date}
GET  /planner/preview                 POST /planner/run
GET  /events?after=&limit=            GET  /events/stream?after=   (SSE, retoma por Last-Event-ID)
```

Exemplo: `curl --unix-socket /run/studyagent/api.sock -H "Authorization: Bearer $(cat ~/.config/studyagent/token)" http://x/summary`

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
