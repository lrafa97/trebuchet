"""Guias do 1.º Katapult por método.

O primeiro Katapult nunca vai para uma board só pelo próprio Katapult: tem de ser
gravado por um método de ROM ou por um programador. Os comandos abaixo seguem os
padrões dos documentos do Klipper (Bootloaders) e do README do Katapult; os que
dependem do teu hardware estão marcados.
"""
from __future__ import annotations

COMMON = """\
Antes de gravar o Katapult:
 - O README do Katapult recomenda apagar o chip por completo antes. Assim o
   Katapult deteta que não há aplicação e entra em bootloader sozinho.
 - Confirma que o katapult.config tem o cristal, a interface (USB/CAN/UART),
   o bitrate CAN e o offset da aplicação certos para ESTA board. Um offset ou
   clock errado deixa a board inutilizável sem programador.
 - O Klipper tem de ser compilado com o offset de bootloader igual ao
   "application offset" do Katapult.
"""

GUIDES: dict[str, str] = {
    "dfu": """\
Método: DFU de ROM (STM32)
 1. Põe a board em DFU (jumper BOOT0/botão BOOT e reset, conforme a tua board).
    Em algumas boards o DFU pode ligar o aquecedor: desliga aquecedores.
 2. O programa confirma que aparece o dispositivo DFU (0483:df11 no lsusb) e corre:
      dfu-util -d 0483:df11 -a 0 -R -D <katapult.bin> -s 0x08000000:leave
    (padrão dos documentos do Klipper; confirma o endereço de início da flash do
     teu chip.)
 3. Tira o jumper BOOT0 e reinicia se a board não arrancar sozinha.
 O README do Katapult só documenta DFU para STM32F042/F072; noutros chips confirma
 no AN2606 da ST que o DFU de ROM existe e que a board o expõe.
""",
    "bootsel": """\
Método: BOOTSEL (RP2040)
 1. Mantém premido BOOT/BOOTSEL e liga a board por USB (ou faz reset).
 2. Aparece um disco USB. O programa copia o katapult.uf2 para lá, se o encontrar
    montado em /media, /run/media ou /mnt; senão copia tu o ficheiro.
    Alternativa documentada: 'make flash' na pasta do Katapult com a board em BOOTSEL.
 3. ATENÇÃO: gravar o Katapult apaga o Klipper. O Klipper tem de ser gravado
    de novo através do Katapult (é o passo seguinte do plano).
""",
    "stm32flash": """\
Método: UART de ROM (STM32F103)
 1. Liga PA10 (Rx do MCU) e PA9 (Tx do MCU) a um adaptador UART de 3,3 V, BOOT0 alto,
    BOOT1 baixo, e faz reset.
 2. Grava (padrão dos documentos do Klipper, trocando o ficheiro):
      stm32flash -w <katapult.bin> -v -g 0 /dev/ttyAMA0
    Num Raspberry Pi a UART tem de ser a completa (a mini UART não suporta a paridade
    que o stm32flash usa).
 3. Volta BOOT0 e BOOT1 a baixo.
 Este passo é manual: o programa não corre stm32flash por ti.
""",
    "stlink": """\
Método: programador ST-Link
 1. Constrói o katapult.bin e grava com o STM32CubeProgrammer (ou o teu fluxo
    com OpenOCD) no endereço de início da flash (0x08000000 nos STM32).
 2. Faz apagar o chip completo antes de gravar.
 Este passo é manual: o programa não controla o ST-Link.
""",
    "deployer": """\
Método: Katapult deployer (substitui um bootloader existente sem programador)
 1. No menuconfig do Katapult, define 'Build Katapult deployment application' com o
    offset do bootloader EXISTENTE. Isto gera out/deployer.bin além do katapult.bin.
 2. Grava o deployer.bin com o bootloader atual (cartão SD, HID, Katapult antigo).
    No fim o deployer reinicia a board em Katapult.
 ATENÇÃO: uma configuração errada deixa a board inutilizável e só um programador a
 recupera. Se vens de um bootloader de fábrica, faz primeiro uma cópia de segurança.
 Este passo é manual.
""",
    "other": """\
Método: outro
 Grava o Katapult com o método que usas nesta board e, quando a board estiver em
 Katapult, confirma no programa.
""",
}


def get_guide(method: str) -> str:
    return GUIDES.get(method, GUIDES["other"]) + "\n" + COMMON
