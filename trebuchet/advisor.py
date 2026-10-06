"""Aconselhamento e plano de gravação.

`advise()` olha para a máquina (boards + perfis + estado de Katapult) e devolve
avisos classificados e um plano ordenado de passos.

Ordem do plano (a regra "bridge por último" é raciocínio de segurança nosso,
não vem definida na documentação do Klipper/Katapult):
  1. parar o serviço Klipper
  2. construir Katapult (só boards sem Katapult) e depois Klipper (todas),
     tudo antes de gravar qualquer coisa
  3. boards sem Katapult: 1.º Katapult (manual/ROM) e logo a seguir o Klipper
  4. boards com Katapult: Klipper
  5. bridge USB-CAN sempre no fim
  6. arrancar o serviço Klipper
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .discover import chip_matches
from .profiles import (SUPPORTED_FAMILIES, BoardProfile, Machine, MachineBoard)

ERROR, WARN, INFO = "erro", "aviso", "info"


@dataclass
class Advice:
    level: str
    text: str
    board: str | None = None


@dataclass
class Step:
    kind: str            # stop_service | build_katapult | build_klipper | first_katapult |
                         # query_uuid | flash_klipper | start_service
    text: str
    board: str | None = None
    manual: bool = False


@dataclass
class Plan:
    advice: list[Advice] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(a.level == ERROR for a in self.advice)


def _can_with_katapult_ok(p: BoardProfile) -> bool:
    """Katapult documenta CAN só em stm32 série F e rp2040."""
    return p.family == "rp2040" or (p.family == "stm32" and p.mcu.lower().startswith("stm32f"))


def advise(machine: Machine, profiles: dict[str, BoardProfile],
           katapult_state: dict[str, str]) -> Plan:
    plan = Plan()
    add = lambda lvl, txt, board=None: plan.advice.append(Advice(lvl, txt, board))

    if not machine.boards:
        add(ERROR, "A máquina não tem boards. Adiciona pelo menos uma.")
        return plan

    resolved: list[tuple[MachineBoard, BoardProfile]] = []
    for mb in machine.boards:
        p = profiles.get(mb.profile_id)
        if p is None:
            add(ERROR, f"perfil '{mb.profile_id}' não existe.", mb.label)
            continue
        for prob in p.problems():
            add(ERROR, f"perfil '{p.id}': {prob}.", mb.label)
        resolved.append((mb, p))

    can_boards = [(mb, p) for mb, p in resolved if p.is_can]
    bridges = [(mb, p) for mb, p in resolved if p.is_bridge]

    # --- verificações por board ---
    for mb, p in resolved:
        state = katapult_state.get(mb.label, "unknown")

        if p.family not in SUPPORTED_FAMILIES:
            add(ERROR, f"família '{p.family}' fora do âmbito da v1 (só stm32 e rp2040).", mb.label)
            continue
        if mb.chip and chip_matches(p.mcu, mb.chip) is False:
            add(ERROR, f"o chip visto na descoberta ({mb.chip}) não corresponde ao perfil '{p.id}' "
                       f"({p.mcu}): perfil errado para esta board. Corrige o perfil, ou apaga o campo "
                       "'chip' da máquina se o texto do perfil estiver só escrito de outra forma.", mb.label)
        if p.interface == "uart" and not mb.device:
            add(ERROR, "board UART sem caminho do dispositivo (campo 'device').", mb.label)
        if p.is_can and not _can_with_katapult_ok(p):
            add(WARN, "a documentação do Katapult só dá CAN para stm32 série F e rp2040; "
                      "confirma que este MCU é suportado.", mb.label)
        if p.is_can and not mb.uuid and state != "no":
            add(ERROR, "falta o UUID CAN. Vê-o no printer.cfg (canbus_uuid) ou liga só esta "
                       "board ao barramento e usa o canbus_query.", mb.label)
        if state == "unknown":
            add(ERROR, "estado do Katapult desconhecido: declara 'sim' ou 'não', ou faz o teste "
                       "ativo (pede bootloader e vê o que aparece; pede confirmação). "
                       "Sem isto não há plano.", mb.label)
        if state == "no":
            if not p.katapult_config.exists():
                add(ERROR, "sem Katapult e o perfil não tem katapult.config.", mb.label)
            add(INFO, f"precisa do 1.º Katapult por '{p.first_katapult_method}' "
                      "(passo manual com o hardware).", mb.label)
            if p.first_katapult_method == "dfu" and p.dfu_heater_warning:
                add(WARN, "em DFU algumas boards podem ligar o aquecedor: desliga aquecedores.", mb.label)
            if p.is_can:
                add(WARN, "board CAN nova: liga só esta ao adaptador enquanto descobres o UUID "
                          "(o 'flashtool -q' só é seguro com um único nó).", mb.label)

    # --- verificações da máquina ---
    if can_boards:
        rates = {p.can_bitrate for _, p in can_boards if p.can_bitrate}
        if len(rates) > 1:
            add(ERROR, f"bitrates CAN diferentes entre perfis ({sorted(rates)}): "
                       "todos os nós de um barramento têm de usar o mesmo.")
        if len(bridges) > 1:
            add(WARN, "há mais de uma bridge USB-CAN na máquina.")
        if not bridges:
            add(INFO, f"sem bridge Klipper: assume adaptador USB-CAN externo com "
                      f"'{machine.can_interface}' já configurado.")
    if bridges:
        add(INFO, "a bridge é gravada por último; ao reiniciar, o Linux desativa o can0 "
                  "(usa allow-hotplug para voltar sozinho).")

    labels = {mb.label for mb, _ in resolved}
    if len(labels) != len(resolved):
        add(ERROR, "há labels de boards repetidos na máquina.")

    only = [mb.label for mb, _ in resolved]
    if len(only) > 1:
        add(INFO, "atualiza todas as boards da máquina juntas para evitar versões misturadas.")

    # --- plano ---
    if plan.has_errors:
        return plan

    needs_katapult = [(mb, p) for mb, p in resolved if katapult_state.get(mb.label) == "no"]
    ordered = sorted(resolved, key=lambda t: t[1].is_bridge)   # bridge no fim, resto estável

    s = plan.steps
    s.append(Step("stop_service", "Parar o serviço Klipper"))
    for mb, p in needs_katapult:
        s.append(Step("build_katapult", f"Construir Katapult para '{mb.label}' ({p.name})", mb.label))
    for mb, p in ordered:
        s.append(Step("build_klipper", f"Construir Klipper para '{mb.label}' ({p.name})", mb.label))

    for mb, p in ordered:
        if katapult_state.get(mb.label) == "no":
            s.append(Step("first_katapult", f"Gravar o 1.º Katapult em '{mb.label}' ({p.first_katapult_method})",
                          mb.label, manual=True))
            if p.is_can and not p.is_bridge and not mb.uuid:
                s.append(Step("query_uuid", f"Descobrir o UUID de '{mb.label}' (só esta board no barramento)",
                              mb.label, manual=True))
        s.append(Step("flash_klipper", f"Gravar Klipper em '{mb.label}'", mb.label))
    s.append(Step("start_service", "Arrancar o serviço Klipper"))
    return plan
