"""Menus: máquinas, perfis, deteção, guias e diagnóstico (estilo KIAUH)."""
from __future__ import annotations

import grp
import importlib.util
import os
import shutil
import sys
from pathlib import Path

from . import __version__, catalog, detect, discover, pinmatch
from .advisor import ERROR, INFO, WARN, Plan, advise
from .builder import edit_config, load_build
from .config import Settings
from .executor import execute_plan
from .flasher import Flasher
from .guides import GUIDES, get_guide
from .profiles import (FAMILIES, FIRST_KATAPULT_METHODS, INTERFACES, KATAPULT_STATES,
                       BoardProfile, Machine, MachineBoard, list_machines, list_profiles,
                       save_machine, save_profile, slugify)
from .registry import Registry
from .runner import Runner
from .ui import TerminalIO

KAT_LABEL = {"yes": "sim", "no": "não", "unknown": "?"}


def _in_group(name: str) -> bool:
    """True se o utilizador pertence ao grupo (ou é root)."""
    if os.geteuid() == 0:
        return True
    try:
        return grp.getgrnam(name).gr_gid in os.getgroups()
    except KeyError:
        return False


class App:
    def __init__(self, settings: Settings, io: TerminalIO | None = None):
        self.s = settings
        self.s.ensure_dirs()
        self.io = io or TerminalIO()
        self.runner = Runner(dry_run=settings.dry_run, log_file=settings.log_file, echo=self.io.say)
        self.reg = Registry(settings.registry_file)
        self.flasher = Flasher(settings, self.runner, self.reg, self.io)

    # ------------------------------------------------------------------ helpers
    def profiles(self) -> dict[str, BoardProfile]:
        return {p.id: p for p in list_profiles(self.s.profiles_dir)}

    def kat_state(self, m: Machine) -> dict[str, str]:
        return {b.label: self.reg.katapult(m.slug, b.label) for b in m.boards}

    def save(self, m: Machine) -> None:
        save_machine(self.s.machines_dir, m)

    def plan_for(self, m: Machine) -> Plan:
        return advise(m, self.profiles(), self.kat_state(m))

    def show_plan(self, plan: Plan) -> None:
        io = self.io
        marks = {ERROR: "[x]", WARN: "[!]", INFO: "[i]"}
        io.say("\nAconselhamento:")
        if not plan.advice:
            io.say("  (sem avisos)")
        for a in plan.advice:
            who = f"[{a.board}] " if a.board else ""
            io.say(f"  {marks[a.level]} {who}{a.text}")
        if plan.steps:
            io.say("\nPlano (por ordem):")
            for i, st in enumerate(plan.steps, 1):
                io.say(f"  {i:>2}. {st.text}" + ("   (manual)" if st.manual else ""))
        elif plan.has_errors:
            io.say("\nSem plano enquanto houver erros.")

    # ------------------------------------------------------------------ principal
    def main(self) -> None:
        while True:
            k = self.io.menu(f"Trebuchet - Klipper Flash Helper v{__version__}", [
                "Máquinas (criar / abrir)",
                "Perfis de board",
                "Detetar dispositivos ligados",
                "Guias do 1.º Katapult",
                "Diagnóstico (ferramentas e caminhos)",
            ], back="Sair")
            if k is None:
                return
            (self.machines_menu, self.profiles_menu, self.detect_menu,
             self.guides_menu, self.doctor)[k]()

    # ------------------------------------------------------------------ máquinas
    def machines_menu(self) -> None:
        while True:
            ms = list_machines(self.s.machines_dir)
            entries = [f"{m.name}  ({len(m.boards)} boards)" for m in ms]
            entries.append("Descobrir o que está ligado e criar a máquina (automático)")
            entries.append("Nova máquina (escolher as boards à mão)")
            k = self.io.menu("Máquinas", entries)
            if k is None:
                return
            if k == len(ms):
                m = self.discover_flow()
                if m:
                    self.machine_menu(m)
            elif k == len(ms) + 1:
                m = self.new_machine()
                if m:
                    self.machine_menu(m)
            else:
                self.machine_menu(ms[k])

    def run_discovery(self, *, ask: bool = True, canbus_query: bool = False,
                      use_moonraker: bool = True) -> "discover.Discovery":
        io = self.io
        if ask:
            use_moonraker = io.confirm("Perguntar ao Moonraker o chip e a versão de cada MCU? "
                                       "(só leitura; precisa do Klipper a correr)", default=True)
            canbus_query = io.confirm("Correr também o canbus_query? (só vê nós CAN ainda sem id; "
                                      "com o Klipper a correr não mostra os já declarados)", default=False)
        found = discover.discover(self.s, self.runner, use_moonraker=use_moonraker,
                                  canbus_query=canbus_query)
        io.title("O que encontrei")
        io.say(discover.render(found))
        return found

    def discover_flow(self) -> Machine | None:
        io = self.io
        found = self.run_discovery()
        if not found.devices:
            io.warn("Não encontrei nenhuma board. Cria a máquina à mão ou liga as boards e tenta de novo.")
            io.pause()
            return None
        if found.bridge_unresolved:
            can = [x for x in found.devices if x.transport == "can"]
            idx = io.choose("Qual destas é a ponte USB-CAN (a board ligada por USB ao Pi)?",
                            [f"{x.name}  ({x.uuid})" for x in can] + ["nenhuma / não sei"])
            if idx is not None and idx < len(can):
                can[idx].bridge = True
        name = io.ask("Nome da máquina (ex.: cliente-voron)")
        if not name:
            return None
        m = Machine(name=name, can_interface=self.s.can_interface)
        profs = list(self.profiles().values())
        io.say("\nAgora diz-me que board é cada uma. O chip serve para filtrar os perfis; "
               "não adivinho o modelo.")
        for x in discover.flash_order(found.devices):
            cands = discover.candidate_profiles(x, profs)
            ident = x.uuid or x.serial or x.device
            opts = [f"{p.name}  [{p.mcu}, {p.interface}]" for p in cands]
            opts += ["Criar perfil novo para esta board", "Ignorar esta board"]
            if not cands:
                io.say(f"\nNão há perfis compatíveis com {x.name} (chip {x.chip or '?'}, "
                       f"{discover.profile_interface_for(x)}).")
            idx = io.choose(f"{x.name}: {x.transport}, chip {x.chip or '?'}, {ident}", opts)
            if idx is None or idx == len(cands) + 1:
                continue
            if idx == len(cands):
                p = self.new_profile(chip=x.chip, interface=discover.profile_interface_for(x),
                                     mcu_name=x.name)
                if p is None:
                    continue
                profs.append(p)
            else:
                p = cands[idx]
            label = slugify(io.ask("Nome desta board na máquina", x.name))
            if m.board(label):
                io.warn("Já existe uma board com esse nome; ignorada.")
                continue
            m.boards.append(MachineBoard(label=label, profile_id=p.id, uuid=x.uuid,
                                         serial=x.serial, device=x.device,
                                         chip=x.chip))
            k = io.ask("Esta board já tem Katapult? (s = sim, n = não, ? = não sei)", "?").lower()
            self.reg.set_katapult(m.slug, label, {"s": "yes", "n": "no"}.get(k[:1], "unknown"))
        if not m.boards:
            io.warn("Nenhuma board ficou na máquina; nada guardado.")
            return None
        self.save(m)
        io.ok(f"Máquina '{m.name}' guardada com {len(m.boards)} boards.")
        self.show_plan(self.plan_for(m))
        return m

    def new_machine(self) -> Machine | None:
        io = self.io
        name = io.ask("Nome da máquina (ex.: cliente-voron)")
        if not name:
            return None
        m = Machine(name=name, can_interface=io.ask("Interface CAN no Pi", self.s.can_interface))
        self.save(m)
        io.say("\nAgora escolhe as boards que a máquina tem.")
        while True:
            if not self.add_board(m):
                break
            if not io.confirm("Adicionar outra board?", default=True):
                break
        self.save(m)
        return m

    def add_board(self, m: Machine) -> bool:
        io = self.io
        profs = list(self.profiles().values())
        opts = [f"{p.name}  [{p.mcu}, {p.interface}]" for p in profs] + ["(criar perfil novo)"]
        idx = io.choose("Que board é?", opts)
        if idx is None:
            return False
        if idx == len(profs):
            p = self.new_profile()
            if not p:
                return False
        else:
            p = profs[idx]
        label = slugify(io.ask("Nome desta board na máquina", p.id))
        if m.board(label):
            io.warn("Já existe uma board com esse nome.")
            return False
        mb = MachineBoard(label=label, profile_id=p.id)
        if p.is_can:
            mb.uuid = io.ask("canbus_uuid (12 hex; vazio se ainda não sabes)").lower()
        elif p.interface == "usb":
            mb.serial = io.ask("Serial USB (parte final do nome em /dev/serial/by-id; vazio se só há uma)")
        elif p.interface == "uart":
            mb.device = io.ask("Caminho do dispositivo UART (ex.: /dev/ttyAMA0)")
        if p.is_bridge:
            mb.serial = io.ask("Serial USB da bridge (vazio se só há uma)")
        m.boards.append(mb)
        k = io.ask("A board já tem Katapult? (s = sim, n = não, ? = não sei; "
                   "o '?' pode ser resolvido com o teste ativo)", "?").lower()
        state = {"s": "yes", "n": "no"}.get(k[:1], "unknown")
        self.reg.set_katapult(m.slug, label, state)
        self.save(m)
        return True

    def machine_menu(self, m: Machine) -> None:
        while True:
            k = self.io.menu(f"Máquina: {m.name}", [
                "Assistente completo (aconselhar → construir → gravar)",
                "Estado das boards",
                "Aconselhamento e plano",
                "Construir firmwares",
                "Gravar (usa as builds guardadas)",
                "Editar boards (adicionar, remover, UUID/serial, Katapult, teste ativo)",
            ])
            if k is None:
                return
            if k == 0:
                self.wizard(m)
            elif k == 1:
                self.show_state(m)
            elif k == 2:
                self.show_plan(self.plan_for(m))
            elif k == 3:
                self.run_phase(m, "build")
            elif k == 4:
                self.run_phase(m, "flash")
            elif k == 5:
                self.edit_boards(m)

    def show_state(self, m: Machine) -> None:
        profs = self.profiles()
        self.io.say(f"\nMáquina '{m.name}'  (CAN: {m.can_interface})")
        self.io.say(f"  {'board':<14}{'perfil':<22}{'interface':<16}{'id':<16}{'Katapult':<9}Klipper gravado")
        for b in m.boards:
            p = profs.get(b.profile_id)
            ident = b.uuid or b.serial or b.device or "-"
            st = self.reg.get(m.slug, b.label)
            self.io.say(f"  {b.label:<14}{b.profile_id:<22}{(p.interface if p else '?'):<16}"
                        f"{ident[:15]:<16}{KAT_LABEL[self.reg.katapult(m.slug, b.label)]:<9}"
                        f"{st.get('klipper_version', '-')}")

    def run_phase(self, m: Machine, phase: str) -> bool:
        plan = self.plan_for(m)
        self.show_plan(plan)
        if plan.has_errors:
            return False
        what = "construir" if phase == "build" else "gravar"
        if self.runner.dry_run:
            self.io.warn("Modo --dry-run: nada é gravado nem serviços alterados "
                         "(as compilações correm na mesma).")
        if not self.io.confirm(f"\nQueres {what} agora?", default=False):
            return False
        return execute_plan(plan, m, self.profiles(), self.s, self.runner, self.reg, self.flasher,
                            self.io, self.save, phase=phase)

    def wizard(self, m: Machine) -> None:
        plan = self.plan_for(m)
        self.show_plan(plan)
        if plan.has_errors:
            self.io.warn("Resolve os erros (menu 'Editar boards') e volta a tentar.")
            return
        if not self.io.confirm("\nSegue este plano?", default=False):
            return
        if not execute_plan(plan, m, self.profiles(), self.s, self.runner, self.reg, self.flasher,
                            self.io, self.save, phase="build"):
            return
        self.io.ok("Tudo construído. Nada foi gravado até agora.")
        plan = self.plan_for(m)                 # o estado pode ter mudado
        if not self.io.confirm("Passar à gravação?", default=False):
            return
        if execute_plan(plan, m, self.profiles(), self.s, self.runner, self.reg, self.flasher,
                        self.io, self.save, phase="flash"):
            self.io.ok("Máquina concluída.")

    def edit_boards(self, m: Machine) -> None:
        while True:
            self.show_state(m)
            idx_k = self.io.menu("Editar boards", [
                "Adicionar board", "Remover board",
                "Alterar UUID / serial / dispositivo",
                "Definir se tem Katapult", "Teste ativo 'tem Katapult?'",
            ])
            if idx_k is None:
                return
            k = "arikt"[idx_k]
            if k == "a":
                self.add_board(m)
                continue
            if not m.boards:
                continue
            idx = self.io.choose("Que board?", [b.label for b in m.boards])
            if idx is None:
                continue
            mb = m.boards[idx]
            p = self.profiles().get(mb.profile_id)
            if k == "r":
                if self.io.confirm(f"Remover '{mb.label}'?"):
                    m.boards.pop(idx)
            elif k == "i":
                mb.uuid = self.io.ask("UUID CAN", mb.uuid).lower()
                mb.serial = self.io.ask("Serial USB", mb.serial)
                mb.device = self.io.ask("Dispositivo", mb.device)
            elif k == "k":
                v = self.io.ask("Tem Katapult? (s/n/?)", "?").lower()
                self.reg.set_katapult(m.slug, mb.label, {"s": "yes", "n": "no"}.get(v[:1], "unknown"))
            elif k == "t" and p:
                self.flasher.active_katapult_test(m, mb, p)
            self.save(m)

    # ------------------------------------------------------------------ perfis
    def profiles_menu(self) -> None:
        while True:
            k = self.io.menu("Perfis de board", ["Listar perfis", "Criar perfil novo",
                                                 "Consultar o catálogo de boards (só leitura)"])
            if k is None:
                return
            if k == 0:
                ps = list_profiles(self.s.profiles_dir)
                if not ps:
                    self.io.say("\nAinda não há perfis. Cria um com a opção 2.")
                for p in ps:
                    probs = p.problems()
                    self.io.say(f"\n  {p.id}: {p.name}  [{p.mcu}, {p.family}, {p.interface}]"
                                f"  1.º Katapult: {p.first_katapult_method}")
                    if p.notes:
                        self.io.say(f"     notas: {p.notes}")
                    for pr in probs:
                        self.io.warn(f"     {pr}")
            elif k == 1:
                self.new_profile()
            else:
                self.pick_catalog_entry()

    def pin_suggestions(self, mcu_name: str, chip_hint: str = "") -> list:
        """Boards do catálogo que usam os pins que o printer.cfg declara para este MCU."""
        cfg = self.s.printer_cfg or ""
        path = discover.find_printer_cfg(cfg)
        if not path:
            return []
        entries = catalog.load_catalog(self.s.klipper_dir, self.s.catalog_file)
        return pinmatch.suggest(path, mcu_name, entries, chip_hint=chip_hint, top=5)

    def show_suggestions(self, matches: list) -> None:
        io = self.io
        best = matches[0].score
        io.say("\nPelos pins do printer.cfg, a board pode ser (é uma sugestão, não uma certeza):")
        for m in matches:
            tie = "  <- empate: os pins não as distinguem" if best - m.score < 0.02 and len(
                [x for x in matches if best - x.score < 0.02]) > 1 else ""
            io.say(f"  {m.score * 100:>3.0f}%  {m.entry.name}  [{', '.join(m.entry.chips)}]"
                   f"  ({m.shared}/{m.total} pins){tie}")

    def pick_catalog_entry(self, chip_hint: str = "", suggestions: list | None = None
                           ) -> "tuple[catalog.CatalogEntry, str] | None":
        """Escolhe uma board do catálogo (Klipper + <dados>/catalog.toml).
        Devolve (entrada, chip escolhido) ou None."""
        io = self.io
        entries = catalog.load_catalog(self.s.klipper_dir, self.s.catalog_file)
        usable = [e for e in entries if e.family in ("stm32", "rp2040")]
        if not usable:
            io.warn(f"Catálogo vazio: não encontrei boards em {self.s.klipper_dir / 'config'}. "
                    "Cria o perfil do zero (ou acrescenta boards em catalog.toml).")
            return None
        hidden = len(entries) - len(usable)
        e = None
        if suggestions:
            self.show_suggestions(suggestions)
            si = io.choose("Qual destas é a tua?",
                           [f"{m.entry.name}  [{', '.join(m.entry.chips)}]" for m in suggestions]
                           + ["Nenhuma destas: escolher a marca e a board à mão (ou criar do zero)"])
            if si is None:
                return None
            if si < len(suggestions):
                e = suggestions[si].entry
        if e is None:
            labels = dict(catalog.VENDORS)
            labels[catalog.OTHER] = "Outras marcas"
            vendors = [v for v in (*(k for k, _ in catalog.VENDORS), catalog.OTHER)
                       if any(x.vendor == v for x in usable)]
            vi = io.choose("Marca da board:", [f"{labels[v]}  ({sum(1 for x in usable if x.vendor == v)})"
                                               for v in vendors] + ["A minha board não está aqui: criar do zero"])
            if vi is None or vi == len(vendors):
                io.say("\nA criar o perfil do zero.")
                return None
            pool = [x for x in usable if x.vendor == vendors[vi]]
            core = discover.chip_core(chip_hint)
            if core:
                same = [x for x in pool if core in {discover.chip_core(c) for c in x.chips}]
                if same:
                    io.say(f"A mostrar só as boards com o chip visto ({core}).")
                    pool = same
            bi = io.choose("Board:", [f"{x.name}   [{', '.join(x.chips)}]" for x in pool]
                           + ["Não é nenhuma destas: criar do zero"])
            if bi is None or bi == len(pool):
                io.say("\nA criar o perfil do zero.")
                return None
            e = pool[bi]
        io.say(f"\nO que o Klipper diz sobre esta board ({Path(e.source).name}):")
        for line in e.notes.splitlines():
            io.say(f"  | {line}")
        if hidden:
            io.say(f"\n({hidden} boards do catálogo ficam fora da v1: AVR, LPC, SAM, ...)")
        chip = e.chip
        if len(e.chips) > 1:
            ci = io.choose("O texto menciona vários chips (a board tem variantes). Qual é o da tua?",
                           list(e.chips))
            if ci is None:
                return None
            chip = e.chips[ci]
        return e, chip

    def new_profile(self, chip: str = "", interface: str = "", mcu_name: str = "") -> BoardProfile | None:
        """`chip` e `interface` vêm da descoberta: quando existem, não se pergunta outra vez."""
        io = self.io
        entry = None
        src = io.choose("Como queres criar o perfil?",
                        ["A partir de uma board conhecida (catálogo do Klipper)", "Do zero"])
        if src is None:
            return None
        if src == 0:
            sugg = self.pin_suggestions(mcu_name, chip) if mcu_name else []
            picked = self.pick_catalog_entry(chip, sugg)
            if picked:
                entry, chip = picked
        name = io.ask("Nome da board (ex.: EBB42 1.2 BTT)", entry.name if entry else "")
        if not name:
            return None
        pid = slugify(io.ask("Identificador do perfil", slugify(name)))
        known_family = ("stm32" if chip.lower().startswith("stm32")
                        else "rp2040" if chip.lower().startswith("rp2040") else "")
        if known_family:
            family = known_family
            io.say(f"Família {family} (do chip visto: {chip}).")
        else:
            fam_i = io.choose("Família do MCU (a v1 só suporta stm32 e rp2040)", list(FAMILIES))
            if fam_i is None:
                return None
            family = FAMILIES[fam_i]
        mcu = io.ask("MCU exato (ex.: stm32f446; só o que tiveres confirmado)",
                     discover.chip_core(chip) or chip)
        if interface in INTERFACES:
            io.say(f"Interface {interface} (do que está ligado).")
        else:
            if entry:
                io.say(f"Palpite pelo nome do ficheiro: {entry.interface_hint}. Confirma no texto acima.")
            if_i = io.choose("Interface com o Pi", list(INTERFACES))
            if if_i is None:
                return None
            interface = INTERFACES[if_i]
        m_i = io.choose("Como se grava o 1.º Katapult nesta board?", list(FIRST_KATAPULT_METHODS))
        if m_i is None:
            return None
        bitrate = int(io.ask("Bitrate CAN", "1000000")) if interface in ("can", "usb-can-bridge") else None
        baud = int(io.ask("Baud rate UART", "250000")) if interface == "uart" else None
        p = BoardProfile(
            id=pid, name=name, mcu=mcu, family=family, interface=interface,
            first_katapult_method=FIRST_KATAPULT_METHODS[m_i], can_bitrate=bitrate, uart_baud=baud,
            dfu_heater_warning=io.confirm("Avisar para desligar aquecedores em DFU?", default=True),
            notes=io.ask("Notas (opcional)", (" ".join(entry.notes.split())[:400]
                                              + f" [fonte: {Path(entry.source).name}]") if entry else ""))
        d = save_profile(self.s.profiles_dir, p)
        self._attach_config(p, d / "klipper.config", "klipper", required=True)
        self._attach_config(p, d / "katapult.config", "katapult", required=False)
        io.ok(f"Perfil '{pid}' guardado em {d}")
        return p

    def _attach_config(self, p: BoardProfile, target: Path, kind: str, required: bool) -> None:
        io = self.io
        repo = self.s.klipper_dir if kind == "klipper" else self.s.katapult_dir
        current = repo / ".config"
        opts = [f"Importar um ficheiro .config que já funciona (caminho)",
                f"Usar o .config atual de {repo} ({'existe' if current.exists() else 'não existe'})",
                f"Abrir o menuconfig do {kind} agora"]
        if not required:
            opts.append("Saltar (sem Katapult neste perfil)")
        idx = io.choose(f"Configuração do {kind} para '{p.id}':", opts)
        if idx is None or (not required and idx == 3):
            return
        if idx == 0:
            src = Path(io.ask("Caminho do .config").strip()).expanduser()
            if src.is_file():
                shutil.copyfile(src, target)
            else:
                io.warn("Ficheiro não encontrado.")
        elif idx == 1:
            if current.exists():
                shutil.copyfile(current, target)
            else:
                io.warn("Não existe .config nessa pasta.")
        else:
            if not edit_config(self.s, self.runner, kind, target):
                io.warn("O menuconfig não gerou o ficheiro.")

    # ------------------------------------------------------------------ deteção
    def detect_menu(self) -> None:
        io = self.io
        io.title("Dispositivos ligados")
        scan = detect.scan_usb(self.runner)
        io.say("\nPortas série USB (/dev/serial/by-id):")
        if not scan.serial:
            io.say("  (nenhuma)")
        for d in scan.serial:
            io.say(f"  {d.app:<9} mcu={d.mcu:<14} serial={d.serial}")
        io.say("\nBootloaders de ROM e Katapult/Klipper por VID:PID:")
        shown = [i for i in scan.ids if i.kind != "other"]
        if not shown:
            io.say("  (nenhum)")
        for i in shown:
            io.say(f"  {i.vidpid}  {i.kind:<15} {i.desc}")
        st = detect.can_status(self.runner, self.s.can_interface)
        io.say(f"\n{self.s.can_interface}: " + ("não existe" if not st.exists else
               f"{'UP' if st.up else 'DOWN'}, bitrate {st.bitrate or '?'}"))
        if st.exists and io.confirm("Correr o canbus_query do Klipper? (só mostra nós ainda não "
                                    "reclamados por um host; pára o Klipper antes)", default=False):
            q = detect.query_can_klipper(self.s, self.runner)
            if not q.ok:
                io.warn(q.output.strip()[-400:])
            for n in q.nodes:
                io.say(f"  uuid={n.uuid}  app={n.app}")
            if q.ok and not q.nodes:
                io.say("  (nenhum nó não inicializado)")
        io.pause()

    # ------------------------------------------------------------------ guias
    def guides_menu(self) -> None:
        keys = list(GUIDES)
        while True:
            idx = self.io.choose("Guia de que método?", keys)
            if idx is None:
                return
            self.io.say("\n" + get_guide(keys[idx]))
            self.io.pause()

    # ------------------------------------------------------------------ diagnóstico
    def doctor(self, interactive: bool = True) -> list[tuple[str, bool, str]]:
        s = self.s
        flashtool = s.find_flashtool()
        checks: list[tuple[str, bool, str]] = [
            ("Python >= 3.8", sys.version_info >= (3, 8), sys.version.split()[0]),
            ("pyserial (python3-serial)", importlib.util.find_spec("serial") is not None,
             "preciso para o flashtool.py em USB/UART"),
            ("git", shutil.which("git") is not None, ""),
            ("make", shutil.which("make") is not None, ""),
            ("arm-none-eabi-gcc", shutil.which("arm-none-eabi-gcc") is not None,
             "compilador dos STM32/RP2040 (ver docs do Klipper)"),
            ("dfu-util", shutil.which("dfu-util") is not None, "1.º Katapult por DFU"),
            ("lsusb", shutil.which("lsusb") is not None, "pacote usbutils"),
            ("ip", shutil.which("ip") is not None, "estado do can0"),
            (f"Klipper em {s.klipper_dir}", (s.klipper_dir / "Makefile").exists(), ""),
            (f"Katapult em {s.katapult_dir}", (s.katapult_dir / "Makefile").exists(), "git clone https://github.com/Arksine/katapult"),
            ("flashtool.py", flashtool is not None, str(flashtool) if flashtool else "não encontrado"),
            ("canbus_query.py", s.canbus_query_script().exists(), str(s.canbus_query_script())),
            (f"klippy-env python", (s.klippy_env / "bin" / "python").exists(),
             "tem o python-can que o canbus_query.py usa; sem ele instala python3-can"),
            ("sudo", shutil.which("sudo") is not None or os.geteuid() == 0,
             "parar/arrancar o Klipper e dfu-util"),
            ("grupo dialout", _in_group("dialout"),
             "acesso às portas série USB sem root (sudo usermod -aG dialout $USER e voltar a entrar)"),
        ]
        self.io.title("Diagnóstico")
        for name, ok, note in checks:
            self.io.say(f"  [{'ok' if ok else '--'}] {name}" + (f"   {note}" if note else ""))
        self.io.say(f"\nDados em: {s.data_dir}")
        if interactive:
            self.io.pause()
        return checks
