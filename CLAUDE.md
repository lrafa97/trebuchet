# Trebuchet: ficha de contexto para o Claude

## Repositório público: regra obrigatória

Este repositório é público, incluindo este ficheiro, os commits e o histórico. Vale para qualquer sessão do Claude, na web ou no PC.

- Nunca publicar nomes de pessoas, nomes reais de máquinas, IPs reais, endereços MAC, números de série, nomes de redes Wi-Fi, caminhos de pastas pessoais, emails, tokens, palavras-passe, nomes de clientes, projetos de trabalho ou notas sobre o utilizador.
- Usar sempre exemplos genéricos: `printer`, `192.168.1.50`, `<MAC>`, "máquina de teste".
- Antes de cada commit, rever o conteúdo a publicar. Se tiver dados destes, substituí-los antes do push.

Lê isto antes de mexer no código. Programa: `treb` / `trebuchet`. Repositório: github.com/lrafa97/trebuchet.
A fonte de verdade é o GitHub do utilizador, não qualquer cópia local do Claude (o sandbox do Claude é descartável).
Itens marcados com **[assumido]** ou **[não confirmado]** não foram vistos em hardware real nem no repositório atual.

## 1. Objetivo

- **O quê:** programa de terminal, estilo KIAUH (menus numerados), para compilar e gravar **Klipper** e **Katapult** em boards compatíveis com Klipper, a correr no Raspberry Pi da máquina.
- **Para quem:** quem constrói máquinas Klipper e trata do firmware das boards.
- **Porquê:** hoje isto faz-se à mão (menuconfig, `make`, `flashtool.py`, `dfu-util`), com uma ordem delicada em CAN. O programa deve detetar o que está ligado, aconselhar, compilar só o necessário, gravar na ordem certa e verificar depois de cada gravação.
- **Âmbito v1:** MCUs **stm32** e **rp2040**; interfaces USB, CAN, bridge USB-CAN e UART. AVR, SAM, LPC e RP2350 ficam de fora.
- **Requisitos que o utilizador impôs:** funcionar em todos os Pi ainda em uso (não só Pi 5); instalar-se facilmente a partir do GitHub; **funcionar sem qualquer `printer.cfg`**; ter biblioteca de boards (BigTreeTech, Mellow, FYSETC); reconhecer o que está ligado por USB e CAN e propor a ordem de gravação; programa e documentação **em inglês**.
- O utilizador quer também **aprender o porquê de cada passo**, não só os comandos.

## 2. Ambiente

| Coisa | Valor |
|---|---|
| PC de desenvolvimento | Windows com PowerShell. O Ubuntu (WSL) serve para correr os testes. |
| Pi de teste | **Raspberry Pi 3B**, Raspberry Pi OS **Bullseye**, **Python 3.9.2**, utilizador `pi` |
| No Pi | `~/klipper`, `~/katapult` (Arksine), `~/klippy-env` com python-can, serviço Klipper, Moonraker **[assumido]**, `can0` |
| Pi de produção | Pi 5 nas máquinas novas **[assumido: Bookworm, Python 3.11]**. Por isso o código tem de correr em 3.8 a 3.13. |
| Máquina de teste (CAN) | placa EBB42 v1.2 (BTT) por CAN, usada nos testes |
| Hardware seguinte | Mainboard STM32H7 e placas de toolhead, todas por USB. Ainda não testado. |
| Python exigido | **>= 3.8**, só biblioteca padrão. Sem dependências pip. |
| Pacotes apt (instalados pelo `install.sh`) | compilador ARM, `build-essential`, `libncurses-dev`, `dfu-util`, `usbutils`, `iproute2`, `python3-serial`; opcionais `stm32flash`, `python3-can` |
| Dependências externas | Klipper (menuconfig e `canbus_query.py`), Katapult (`flashtool.py`), Moonraker (estado de impressão), `gs_usb` (driver da bridge USB-CAN) |
| KIAUH | só referência de UX. **Nenhum código copiado** (KIAUH e Katapult são GPL-3.0). |


## 3. Arquitetura

Resumo do último estado que o Claude viu; confirmar com `ls trebuchet/ tests/`.

```
install.sh / uninstall.sh / trebuchet.sh   instalar o comando, remover, lançador (trebuchet.sh corre python3 -m trebuchet)
pyproject.toml                             nome trebuchet, requires-python >=3.8, script treb
examples/                                  catalog.toml, machine.toml (marcadores, não valores reais)
trebuchet/
  __main__.py      argparse: update, doctor, scan, discover, plan <máquina>; --dry-run, --data-dir
  menu.py          App: ecrã inicial (versões, estado da impressora, can0), menus Machines, Profiles, Detect, Guides, Doctor; run_plan, run_phase
  update_flow.py   UpdateFlow: o fluxo principal. scan, identify por board, prepare_board (perfil, Katapult, build), plan_and_run, escolha de boards, reparação
  profile_wizard.py  cria perfil com menuconfig; deriva MCU/interface/bitrate do .config; ensure_katapult_config
  matching.py      sugestões por board: lembrada, perfil guardado, catálogo por pins, catálogo por MCU/família
  catalog.py       catálogo lido dos cabeçalhos de ~/klipper/config/generic-*.cfg e sample-*.cfg (+ catalog.toml do utilizador)
  pinmatch.py      compara pinos do printer.cfg com o catálogo; só sugere
  kconfig.py       lê .config do Klipper/Katapult; verifica contra o perfil; compara offsets
  printguard.py    pergunta ao Moonraker o print_stats; bloqueia durante impressão
  discover.py      scan: USB by-id, DFU/BOOTSEL, can0, Moonraker, canbus_query opcional; ordem de gravação
  detect.py        deteção de baixo nível (USB, can0)
  advisor.py       aconselhamento e plano (erros bloqueiam)
  executor.py      corre o plano por fases (build, flash), pára à primeira falha
  builder.py       compila (cópia do .config; build.json com versão e sha256)
  flasher.py       parar o Klipper, gravar, teste ativo do Katapult, verificar
  registry.py      estado em JSON: Katapult sim/não/unknown, última gravação, identidades de boards
  profiles.py      BoardProfile, Machine, MachineBoard (TOML)
  tomlmini.py      leitor/escritor de TOML mínimo (o tomllib só existe no 3.11+)
  config.py        caminhos e trebuchet.cfg
  runner.py        executa comandos; --dry-run; registo em logs; comandos "mutating"
  ui.py            TerminalIO: menus numerados, cores, tabelas
  guides.py        guias do 1.º Katapult
tests/             test_core.py, test_flow.py, test_kconfig.py, test_printguard.py, fixtures/kconfig/*.config (reais)
.github/workflows/tests.yml   CI: Python 3.8, 3.9, 3.11 e 3.13, mais shellcheck
```

Dados do utilizador no Pi: `~/trebuchet_data/` com `profiles/<id>/{profile.toml,klipper.config,katapult.config}`, `machines/<nome>.toml`, `builds/<máquina>/<board>/<klipper|katapult>/`, `registry.json`, `logs/trebuchet.log`, `trebuchet.cfg`.

Fluxo: ecrã inicial, "Update a machine", scan, **por board** (identificar, perfil, Katapult sim/não/testar, menuconfig do Katapult se falta, compilar, resumo), plano, escolha de boards (todas, uma, várias), "Flash now?", gravar por ordem com a bridge no fim.

## 4. Instalar, atualizar e testar

**Pi (primeira vez):**
```bash
git clone https://github.com/lrafa97/trebuchet.git ~/trebuchet
cd ~/trebuchet && ./install.sh --dry-run && ./install.sh
treb doctor
treb --dry-run
```
Se aparecer `treb: command not found`: o `install.sh` pôs o comando em `~/.local/bin`, que pode não estar no PATH da sessão. Corre `export PATH="$HOME/.local/bin:$PATH"` ou sai e entra. Alternativa: `~/trebuchet/trebuchet.sh`.

**Atualizar no Pi:** `cd ~/trebuchet && git pull && ./install.sh`. Sempre `treb --dry-run` primeiro.

**Testes (qualquer máquina, na raiz do repositório):**
```bash
python3 -m unittest discover -s tests -v
python3.8 -m unittest discover -s tests; python3.9 ...; python3.13 ...
shellcheck install.sh uninstall.sh trebuchet.sh
```
No GitHub (v0.2.0) são 80 testes, todos a passar em Python 3.14 no Ubuntu. O `test_flow.py` importa de `tests.test_core`, por isso corre-se sempre a partir da raiz.

**Entrega de alterações (como o Claude trabalha com o utilizador):**
1. O Claude trabalha num clone, faz commit e gera `git format-patch`.
2. O utilizador aplica no PowerShell com **nomes completos** (o PowerShell não expande `*`): `git am <ficheiro>.patch`, depois `git push`.
3. No Pi: `cd ~/trebuchet && git pull && treb --dry-run`.
4. Se o Claude puder fazer push diretamente (token válido), faz `git pull --rebase` antes e nunca `--force`.

**O que precisa de hardware real (nada disto está confirmado):** compilar e gravar de verdade; DFU e BOOTSEL; CAN (`canbus_query.py`, `flashtool.py -q/-r`); teste ativo do Katapult; verificação pós-gravação; formatos do Moonraker; driver `gs_usb` da bridge; deteção do chip nas boards em DFU.
**Primeiro teste real:** numa board de bancada que não seja de cliente, com `--dry-run` primeiro, e só depois a gravar. Os `~/trebuchet_data/{machines,profiles}` antigos de testes têm perfis errados (ex.: MCU `smt32g0b1`); apagar antes de testar.

## 5. Decisões técnicas e porquê

- **Python >= 3.8, só stdlib, `tomlmini` próprio.** O Pi 3B/Bullseye tem 3.9.2. A primeira versão exigia 3.11 e falhou no Pi. Rejeitado: `tomllib`/`tomli` (3.11+ ou pip; o pip é bloqueado pelo PEP 668 em Bookworm). Usa-se `from __future__ import annotations`.
- **MCU, interface e bitrate vêm do `.config` do menuconfig**, não se escrevem. Foi o erro `smt32g0b1` que fez abandonar a escrita. Símbolos verificados com builds reais: `CONFIG_MCU`, `USBSERIAL`→usb, `CANSERIAL`→can, `USBCANBUS`→usb-can-bridge, `SERIAL`→uart, `CANBUS_FREQUENCY`, `FLASH_APPLICATION_ADDRESS` (Klipper) e `LAUNCH_APP_ADDRESS` (Katapult).
- **Offset Klipper = offset Katapult é verificado** (erro no plano). Antes não era.
- **Catálogo lido do próprio Klipper** (cabeçalhos dos `config/generic-*.cfg`), porque acompanha a versão instalada e funciona sem `printer.cfg`. Rejeitado: tabela própria de offsets/clocks por board (a documentação não a tem e varia por board).
- **Sugestões por pinos só sugerem.** Revisões da mesma board têm pinos iguais e empatam; quem decide é o utilizador.
- **O chip mostrado é o que o firmware em execução reporta** (nome by-id, Moonraker), não lido do silício. Uma board em DFU não o reporta; por isso pergunta-se uma vez STM32 ou RP2040 (Enter = STM32).
- **Katapult só onde falta.** Regravá-lo numa board que já o tem é o passo mais arriscado. Estado `unknown` bloqueia o plano até responderes ou fazeres o teste ativo (que não existe em `--dry-run`).
- **Perfil por board; a máquina é a escolha de perfis.** Compila-se no fim de cada board criada (Katapult só se falta, depois Klipper) e as builds ficam guardadas; **gravar é um passo à parte** e pode ser uma board, várias ou todas (por defeito todas, com aviso de versões misturadas se não forem todas). Rejeitado: compilar tudo no fim (era o comportamento antigo; o utilizador escolheu o novo).
- **Duas confirmações antes de gravar:** "Flash now?" e uma por board (board, interface, ficheiro). Mantido de propósito.
- **Ordem de gravação:** boards fora do CAN, nós CAN, **bridge por último**. É raciocínio nosso, não está na documentação. A ordem de identificação não importa.
- **Pára à primeira falha** e lista o que ficou por fazer; oferece voltar a arrancar o Klipper.
- **Nunca para o Klipper durante uma impressão** (Moonraker `print_stats.state` printing/paused). Moonraker mudo nunca é tratado como "parado": pergunta, com resposta por defeito "não".
- **`--dry-run` suprime só os comandos `mutating=True`;** as compilações correm na mesma.
- **`flashtool.py -q` só com um nó CAN ligado** (aviso do README do Katapult).
- **Aviso de aquecedores em DFU:** uma linha, sem confirmação, desligável por perfil (`dfu_heater_warning = false`). A documentação do Katapult cita a Octopus Pro v1 como caso em que o DFU liga o aquecedor **[confirmar na documentação oficial se se aplica à board em uso]**.
- **Menus só numéricos, 0 = voltar, Enter aceita a opção recomendada.** Escrever texto é último recurso. Rejeitado: letras misturadas com números.
- **Erros do plano não são beco sem saída:** o menu oferece refazer o perfil da board ou removê-la da máquina e volta a planear.
- **Programa, mensagens e README em inglês.** O `catalog.toml` aceita `outras` como alias antigo de `other`.
- **Máquinas guardadas continuam a existir** (decidido pelo utilizador), para atualizar sem novo scan.

## 6. Não repetir

- **Exigir Python 3.11 no `install.sh`** partiu o Pi 3B. O mínimo é 3.8.
- **`treb: command not found`** depois de instalar: PATH sem `~/.local/bin`. Não é bug do programa.
- **Perguntar o MCU por escrito** deixou passar `smt32g0b1`. O MCU lê-se do `.config` e `BoardProfile.problems()` valida o chip.
- **Perguntas por fases** (todas as boards, depois todos os Katapult, depois todos os menuconfig) confundiram o utilizador. É sempre uma board completa de cada vez.
- **Prompts redundantes** (nome do perfil, depois MCU, depois identificador que já se sabia). Antes de perguntar, ver se o programa já sabe.
- **Menus com letras e números misturados.** Só números.
- **Beco sem saída em `--dry-run`:** o teste ativo do Katapult não corre, o estado ficava `unknown` e o plano bloqueava. Agora não se oferece o teste em dry-run e pergunta-se Sim/Não.
- **Perfis e máquinas de versões antigas** ficam em `~/trebuchet_data` com dados errados e geram erros no plano. Apagar ou refazer.
- **`git am 0001-*.patch` no PowerShell falha** (não expande `*`). Dar o nome completo, e vários patches têm o mesmo prefixo `0001`.
- **Entregar patches sem todos os ficheiros** deixou o utilizador sem alguns; confirmar a ordem e que todos foram enviados.
- **`Author identity unknown`** ao fazer commit no Windows: `git config user.name lrafa97` e `git config user.email lrafa97@users.noreply.github.com`.
- **No Windows os `.sh` aparecem modificados** no `git status`; o modo no índice é 100755. Não perseguir.
- **Dataclass com campo sem valor depois de campo com valor** (CatalogEntry): `chips` foi para o fim.
- **Nomes do catálogo cortados** (ex.: "v1.1" cortado no ponto): o nome vem do nome do ficheiro (`pretty_name`).
- **`.config` falsos nos testes** têm de ser válidos (MCU, interface, bitrate, offsets coerentes); senão o advisor dá erro. Usar `fake_config(...)` em `tests/test_core.py`.
- **Comparar bitrate CAN** só quando perfil e `.config` são ambos CAN/bridge, senão há ruído.
- **O sandbox do Claude perde-se** (já aconteceu). Antes de trabalhar: clonar ou fazer pull do GitHub e confirmar `git log`. Anexos antigos do utilizador (`kfh.sh`, `pyproject.toml` com `kfh`/3.11, README em português) **são de uma versão antiga**; não os usar como estado atual.

## 7. Convenções

- **Idioma do programa:** inglês (menus, avisos, README, `install.sh`, ajuda do argparse). **Conversa com o utilizador:** português europeu.
- **Documentação:** esta ficha e notas pessoais em português; README em inglês.
- **Commits:** primeira linha curta no imperativo, em inglês (os primeiros commits do repositório estão em português; os recentes em inglês); corpo com o porquê. Terminam com as linhas de atribuição que a sessão indicar.
- **Código:** `from __future__ import annotations` em todos os módulos; só stdlib; sem tabela de valores de hardware inventados; mensagens ao utilizador via `ui.TerminalIO`; testes sem hardware com respostas simuladas.
- **Forma de trabalhar:**
  - em configurações, hardware ou segurança, dizer **o que se assumiu e o que confirmar na documentação oficial**;
  - mostrar o que está por verificar em máquina real;
  - fluxos simples, sem prompts redundantes; para decisões de desenho, apresentar opções fechadas com as consequências.
- **Segurança:** nunca gravar sem plano e confirmação; nunca durante uma impressão; o primeiro teste real é numa board de bancada com `--dry-run`.

## 8. Estado

**Versão no GitHub: v0.2.0**, commit `9730e02`, "One flow: scan, identify, plan, build, flash (v0.2)". Os 4 patches seguintes **não estão no repositório** (confirmado no código em 2026-11-06). Partes das secções 3, 5 e 6 descrevem o comportamento depois destes patches:
1. "Errors are no longer a dead end; no Katapult test offered in dry-run"
2. "Finish each board before starting the next"
3. "Choose which boards to update; ask the chip family for boards in DFU"
4. "Build at the end of each board; flash one, several or all boards"

**Feito (testado só com testes simulados e `--dry-run`):**
- fluxo único "Update a machine"; perfil por board;
- MCU/interface/bitrate lidos do `.config`; verificação do offset Klipper/Katapult;
- catálogo de boards a partir do Klipper, sugestão por pinos e por MCU/família; funcionamento sem `printer.cfg`;
- boards lembradas por UUID CAN / serial USB; deteção da bridge via `gs_usb`;
- guarda de impressão via Moonraker; ecrã inicial com estado;
- tudo em inglês; README reescrito; testes a passar em Python 3.8, 3.9 e 3.13; shellcheck limpo.

**Confirmado em máquina real (Pi 3B, Bullseye, Python 3.9.2):**
- `install.sh` corre; `treb doctor` dá tudo `[ok]` (Klipper, Katapult, `flashtool.py`, `canbus_query.py`, klippy-env, sudo, dialout);
- os menus aparecem e a criação de perfil a partir da lista do Klipper chega ao menuconfig;
- em `--dry-run` com a máquina de teste CAN, o comando `flashtool.py -i can0 -u <uuid> -r` é mostrado e não corre.
**Nada mais.** Não houve compilação completa, gravação, DFU, BOOTSEL, CAN real, nem teste no hardware seguinte.

**Por fazer:**
- primeiro teste real numa board de bancada (compilar, gravar, verificar);
- preparar o hardware seguinte: mainboard STM32H7 e placas de toolhead por USB **[confirmar na documentação oficial: suporte do Katapult ao STM32H7, offsets, como entrar em DFU/bootloader em cada placa]**;
- escolher a licença (decisão do utilizador; sem licença é "todos os direitos reservados");
- traduzir os comentários em português que sobram em `tests/test_core.py` (opcional);
- recuperar os patches 1 a 4 e aplicá-los pela ordem acima, ou refazer essas alterações.

**Problemas conhecidos / não confirmado (confirmar antes de confiar):**
- formatos de resposta do Moonraker (`print_stats`, `mcu_constants.MCU`, `mcu_version`, `CANBUS_BRIDGE`): suposições, testadas só com respostas inventadas; se faltar um campo o chip aparece como `?`;
- driver `gs_usb` da bridge USB-CAN (lido de `/sys/class/net/<iface>/device/driver`);
- nomes dos pacotes apt; que o `dfu-util` precise de `sudo`; mensagem `Programming Complete` e formatos do `-q` e do `canbus_query.py`;
- que um nó CAN recém-gravado apareça no `canbus_query.py` (a verificação pós-gravação assume que sim);
- IDs USB `0483:df11` (DFU STM32) e `2e8a:0003` (BOOTSEL RP2040): conhecimento geral; o Katapult pode personalizá-los;
- onde está o `flashtool.py` (procura em 3 sítios, `~/katapult` primeiro);
- pinos: só testado com configs de exemplo do Klipper, não com um `printer.cfg` real;
- o chip reportado não é lido do silício; uma board em DFU não o reporta;
- "bridge por último" é raciocínio nosso.
