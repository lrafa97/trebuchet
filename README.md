# Trebuchet - Klipper Flash Helper

**Atualiza as boards Klipper com a ordem certa, sem escreveres os comandos.**

Menu numerado (estilo KIAUH) para correr no Raspberry Pi: escolhes as boards do setup,
o programa aconselha, constrói o Katapult (só onde faz falta) e o Klipper, e só depois grava,
com a ordem certa para máquinas CAN.

**Estado: v0.1, nunca correu contra hardware real.** Testado com parsers, repositórios simulados,
uma sessão completa em `--dry-run` e uma instalação real com clones do Katapult e do Klipper.
Antes de gravar uma máquina de cliente, faz o primeiro teste numa board de bancada.

## Instalação no Raspberry Pi

```bash
git clone https://github.com/lrafa97/trebuchet.git ~/trebuchet
cd ~/trebuchet
./install.sh --dry-run     # vê o que vai fazer, sem alterar nada
./install.sh               # instala
treb doctor                # verifica ferramentas e caminhos
treb                       # abre o menu
```

Corre como o **utilizador normal** que usa o Klipper (não root); o script usa `sudo` quando precisa.
Para atualizar o Trebuchet: `cd ~/trebuchet && git pull && ./install.sh`.
Os comandos instalados são `treb` e `trebuchet` (o mesmo programa). Para os remover: `./uninstall.sh` (não apaga os teus dados nem o Klipper/Katapult).

### O que precisas de ter

| Coisa | Quem trata | Notas |
|---|---|---|
| **Python 3.11+** | já vem no sistema | O Pi 5 exige Raspberry Pi OS Bookworm ou mais recente, que traz o 3.11. O Katapult **não** instala Python: o `flashtool.py` usa o `python3` do sistema. |
| **pyserial** | `install.sh` (apt `python3-serial`) | Pedido pelo README do Katapult para USB/UART. Vem do apt porque no Bookworm o pip recusa instalar no Python do sistema. |
| **Klipper** (código-fonte) | tu, ou `--with-klipper-source` | Precisa de existir `~/klipper`. Se já usas o Klipper, está lá. Sem ele o Trebuchet não compila firmware. |
| **Katapult** | `install.sh` (clona para `~/katapult`) | Não mexe numa instalação que já exista (`--update` faz `git pull`). |
| Compilador ARM, `make`, `libncurses-dev` | `install.sh` (apt) | `gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi build-essential libncurses-dev` |
| `dfu-util`, `usbutils`, `iproute2` | `install.sh` (apt) | 1.º Katapult por DFU, `lsusb`, estado do `can0` |
| `stm32flash`, `python3-can` | `install.sh` (apt, opcionais) | UART de ROM; `canbus_query.py` quando não há `klippy-env` |
| grupo `dialout` | `install.sh` | Acesso às portas série USB sem root. É preciso sair e voltar a entrar. |

O Trebuchet em si **não tem dependências pip**: usa só a biblioteca padrão do Python.

O que o instalador **não** faz: instalar o serviço Klipper, configurar o `can0` (depende da máquina;
ver `docs/CANBUS.md` do Klipper) nem tocar em instalações existentes.

Opções do `install.sh`: `--dry-run`, `--yes`, `--no-apt`, `--update`, `--with-klipper-source`,
`--klipper-dir DIR`, `--katapult-dir DIR`. Caminhos diferentes do padrão ficam em `~/trebuchet_data/trebuchet.cfg`:

```ini
[paths]
klipper_dir = /home/pi/klipper
katapult_dir = /home/pi/katapult
klippy_env = /home/pi/klippy-env
[can]
interface = can0
```

## Âmbito da v1

- MCUs **stm32** e **rp2040** (o que o Katapult documenta; CAN só em stm32 série F e rp2040).
- Interfaces: USB, CAN, bridge USB-CAN, UART.
- AVR, SAM, LPC e afins ficam de fora (usam avrdude/bossac/OpenOCD, ver a página Bootloaders do Klipper).

## Fluxo

1. **Perfis de board** (uma vez por modelo): MCU, interface, método do 1.º Katapult, bitrate CAN,
   e os ficheiros `.config` do Klipper (e do Katapult) que **já funcionam** nas tuas máquinas.
   Podes importar um `.config`, usar o atual de `~/klipper` ou abrir o menuconfig.
2. **Nova máquina**: escolhes as boards do setup, identificas cada uma (UUID CAN / serial USB)
   e dizes se já tem Katapult (sim / não / não sei).
3. **Assistente completo**:
   - aconselhamento e plano (erros bloqueiam o plano);
   - **constrói** Katapult (só boards sem ele) e Klipper, tudo antes de gravar qualquer coisa;
   - **grava**: 1.º Katapult nas boards que precisam, depois Klipper, **bridge por último**;
   - verifica depois de cada gravação e **pára à primeira falha**.

`--dry-run` não grava boards nem mexe no serviço Klipper (as compilações correm na mesma).
A compilação corre em `~/klipper` e `~/katapult` e apaga a pasta `out/` de cada um; o `.config`
do perfil nunca é alterado (a build usa uma cópia).
Comandos úteis sem menu: `treb doctor`, `treb scan`, `treb plan <máquina>`.

## Decisões de segurança

- **Katapult só onde falta.** Regravar o Katapult numa board que já o tem é o passo mais arriscado.
- **Estado "não sei" bloqueia o plano** até declarares ou fazeres o teste ativo (pede o bootloader
  ao Klipper e vê se reaparece como Katapult ou como DFU/BOOTSEL; pede confirmação).
- **`flashtool.py -q` só com um nó CAN ligado** (aviso do README do Katapult: com vários nós podem
  surgir erros de transmissão e um nó entrar em "bus off"). O programa recusa correr o `-q` sem confirmação.
- **Aviso de aquecedores em DFU**: uma linha, sem confirmação extra; desligável por perfil
  (`dfu_heater_warning = false`).
- **Cada gravação mostra board, interface e ficheiro e pede confirmação.**
- Se faltar o compilador ARM, o programa avisa antes de tentar compilar.

## Ficheiros

```
~/trebuchet_data/
  profiles/<id>/profile.toml, klipper.config, katapult.config
  machines/<nome>.toml
  builds/<máquina>/<board>/<klipper|katapult>/   (+ build.json com versão e sha256)
  registry.json                                  (estado: Katapult sim/não, última gravação)
  logs/trebuchet.log                                   (todos os comandos executados)
```

Exemplos em `examples/`. Os valores são só marcadores: não há tabela de offsets nem clocks
por board, porque a documentação não a tem e dependem da board. Usa os `.config` que já funcionam.

## O que foi verificado e o que não

Verificado contra o código real (Klipper e Katapult clonados pelo instalador):
- `make KCONFIG_CONFIG=<ficheiro> olddefconfig` e `clean` funcionam com um `.config` fora do repositório
  e não criam `~/klipper/.config`.
- Todas as flags usadas do `flashtool.py` existem: `-d -b -i -f -u -q -r -s`.
- A instalação (`install.sh`) com clones reais, e a segunda execução sem alterar nada.

**Não verificado, confirma antes de confiar:**
- Nada foi testado com boards reais, CAN real, nem com uma compilação completa e gravação.
- Os nomes dos pacotes apt: baseados no instalador do Klipper; confirma com `./install.sh --dry-run` e `apt-get install --dry-run` no Pi.
- Que `dfu-util` precisa de `sudo` (o programa usa `sudo` quando não és root; com regras udev podes tirá-lo).
- Mensagem `Programming Complete` do flashtool e formatos do `-q` e do `canbus_query.py`: lidos no código-fonte, podem mudar.
- Se parar o serviço Klipper faz os nós voltarem a aparecer no `canbus_query.py`: a documentação só diz
  que nós já configurados não aparecem. A verificação pós-gravação assume que um nó acabado de gravar aparece.
- Caminho do `flashtool.py` dentro do Klipper (`lib/katapult/...`): palpite; o programa procura em 3 sítios
  (e usa o do `~/katapult` primeiro).
- IDs `0483:df11` (DFU STM32) e `2e8a:0003` (BOOTSEL RP2040) e o disco `RPI-RP2`: conhecimento geral.
  O Katapult pode ter VID/PID/serial personalizados no menuconfig.
- Se o pedido de bootloader exige alguma opção no menuconfig do Klipper: as páginas lidas não mencionam nenhuma.
- A regra "bridge por último" é raciocínio nosso, não está na documentação.
- O programa **não verifica** que o offset de bootloader do Klipper coincide com o offset da aplicação do Katapult.

## Testes

```bash
python3 -m unittest discover -s tests -v
```

A CI do GitHub (`.github/workflows/tests.yml`) corre os testes em Python 3.11, 3.12 e 3.13 e o shellcheck.

## Licença

Por definir (sem licença, o código é "todos os direitos reservados"; escolhe uma antes de o tornares
público). Não foi copiado código do KIAUH. O KIAUH e o Katapult são GPL-3.0: se um dia
copiares código deles para aqui, o resultado distribuído tem de ser GPL-3.0.
