#!/usr/bin/env python3
"""
Filtre i terminalen med curses.

Bruk:
    python3 filtree.py [katalog ...]

    Du kan oppgi flere kataloger; hver blir en egen rot i treet.

Filter (tekstfeltet på første linje):
    Bare skriv       Kun stier som inneholder teksten vises (ikke store/små bokstaver).
                     Treff vises sammen med mappene de ligger i.
    Backspace        Slett siste tegn         Ctrl+U   Tøm hele feltet
    Ctrl+W           Slett siste ord          Esc      Tøm filteret (avslutter når tomt)
    Tomt felt        Hele treet vises igjen, som før du begynte å filtrere

Betjening:
    Enter            Åpne/lukk katalog REKURSIVT (alt under), åpne fil i vim
    Ctrl + pil ned   Åpne katalog ett nivå
    Ctrl + pil opp   Lukke katalog ett nivå
    Høyre / venstre  Som Ctrl + ned / opp (venstre går til forelder på filer)
    Museklikk        Åpne/lukk katalog ett nivå, åpne fil i vim
    Ctrl + museklikk Åpne/lukk katalog rekursivt
    Musehjul         Rull opp / ned
    Piltaster        Flytt markering (opp/ned)
    PageUp/PageDown  Rull en side           Home / End   Første / siste
    Esc (tomt felt) eller Ctrl+C   Avslutt
"""

import curses
import locale
import os
import subprocess
import sys
from collections import deque

# Sikkerhetsgrense for rekursiv ekspandering (unngår at f.eks. / henger terminalen)
MAX_RECURSIVE_NODES = 20000

# Grense for hvor mange oppføringer filteret leser inn for å lete etter treff
MAX_FILTER_NODES = 50000

# Escape-sekvenser for Ctrl+pil, i tilfelle ncurses ikke gjenkjenner dem selv.
# xterm/VTE/kitty/tmux: ESC [ 1 ; 5 A/B   eldre xterm: ESC [ 5 A/B   rxvt: ESC O a/b
CTRL_UP_SEQS = {"[1;5A", "[5A", "Oa"}
CTRL_DOWN_SEQS = {"[1;5B", "[5B", "Ob"}

KEY_CTRL_U = 21
KEY_CTRL_W = 23


class Node:
    """En fil eller katalog i treet. Barn leses først inn når katalogen åpnes."""

    def __init__(self, path, parent=None, depth=0, label=None):
        self.path = path
        self.lpath = path.lower()  # brukes av filteret
        self.parent = parent
        self.depth = depth
        self.name = label or os.path.basename(path.rstrip(os.sep)) or path
        # Følger ikke symlenker til kataloger, for å unngå evige løkker
        self.is_dir = os.path.isdir(path) and not os.path.islink(path)
        self.is_link = os.path.islink(path)
        self.expanded = False
        self.children = None  # None = ikke lest inn ennå
        self.error = None

    def load_children(self):
        if self.children is not None or not self.is_dir:
            return
        self.children = []
        try:
            entries = os.listdir(self.path)
        except OSError as e:
            self.error = e.strerror or str(e)
            return

        nodes = [Node(os.path.join(self.path, name), self, self.depth + 1)
                 for name in entries]
        # Kataloger først, deretter alfabetisk uten hensyn til store/små bokstaver
        nodes.sort(key=lambda n: (not n.is_dir, n.name.lower()))
        self.children = nodes

    def expand(self):
        if self.is_dir:
            self.load_children()
            self.expanded = True

    def collapse(self):
        self.expanded = False

    def toggle(self):
        if self.is_dir:
            if self.expanded:
                self.collapse()
            else:
                self.expand()

    def expand_recursive(self, limit=MAX_RECURSIVE_NODES):
        """Ekspanderer denne katalogen og alle underkataloger.

        Returnerer True hvis alt ble åpnet, False hvis grensen `limit`
        for antall innleste oppføringer ble nådd (treet er da delvis åpnet).
        """
        budget = limit
        stack = [self]
        while stack:
            node = stack.pop()
            if not node.is_dir:
                continue
            node.expand()
            children = node.children or []
            budget -= len(children)
            if budget < 0:
                return False
            stack.extend(children)
        return True

    def is_fully_expanded(self):
        """True hvis denne katalogen og alle underkataloger under den er åpne."""
        stack = [self]
        while stack:
            node = stack.pop()
            if not node.is_dir:
                continue
            if not node.expanded:
                return False
            stack.extend(node.children or [])
        return True

    def collapse_recursive(self):
        """Kollapser denne katalogen og alle underkataloger som er lest inn."""
        stack = [self]
        while stack:
            node = stack.pop()
            node.expanded = False
            if node.children:
                stack.extend(c for c in node.children if c.is_dir)


def iter_nodes(roots):
    """Går gjennom alle noder som er lest inn så langt."""
    stack = list(roots)
    while stack:
        node = stack.pop()
        yield node
        if node.children:
            stack.extend(node.children)


def load_all(roots, limit=MAX_FILTER_NODES):
    """Leser inn katalogene under røttene, bredde først, slik at filteret kan
    finne treff i kataloger som ennå ikke er åpnet.

    Returnerer True hvis alt ble lest inn, False hvis grensen ble nådd.
    """
    budget = limit
    queue = deque(roots)
    while queue:
        node = queue.popleft()
        if not node.is_dir:
            continue
        if node.children is None:
            node.load_children()
            budget -= len(node.children)
            if budget < 0:
                return False
        queue.extend(node.children)
    return True


def compute_filter(roots, text):
    """Finner noder hvis sti inneholder `text` (uavhengig av store/små bokstaver).

    Returnerer (synlige, treff): `treff` er nodene som selv matcher, `synlige`
    er treffene pluss alle mapper de ligger i (så treet henger sammen).
    """
    needle = text.lower()
    matches = set()
    visible = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if needle in node.lpath:
            matches.add(node)
            n = node
            while n is not None and n not in visible:
                visible.add(n)
                n = n.parent
        if node.children:
            stack.extend(node.children)
    return visible, matches


class Filter:
    """Tilstanden til tekstfilteret."""

    def __init__(self):
        self.text = ""
        self.visible = None     # noder som vises (treff + mapper de ligger i)
        self.matches = None     # noder som selv matcher
        self.complete = True    # False hvis innlesingen stoppet på grensen
        self._snapshot = None   # hvilke noder som var åpne før filteret ble aktivt

    @property
    def active(self):
        return bool(self.text)

    def apply(self, roots, on_scan=None):
        """Beregner filteret på nytt etter at teksten er endret."""
        if self.text:
            if self._snapshot is None:
                # Første tegn: husk hvordan treet så ut, og les inn treet for å lete
                self._snapshot = {n for n in iter_nodes(roots) if n.expanded}
                if on_scan:
                    on_scan()
                self.complete = load_all(roots)
            self.visible, self.matches = compute_filter(roots, self.text)
            # Mapper som bare vises fordi de inneholder treff, åpnes så treffene synes
            for node in self.visible:
                if node.is_dir and node not in self.matches:
                    node.expanded = True
        elif self._snapshot is not None:
            # Tomt felt: gjenopprett treet slik det var før filtreringen
            for node in iter_nodes(roots):
                node.expanded = node in self._snapshot
            self._snapshot = None
            self.visible = None
            self.matches = None
            self.complete = True


def flatten(node, out, visible=None):
    """Lager en flat liste over alle synlige noder (dybde først)."""
    out.append(node)
    if node.expanded and node.children:
        for child in node.children:
            if visible is None or child in visible:
                flatten(child, out, visible)


def visible_nodes(roots, visible=None):
    out = []
    for root in roots:
        if visible is None or root in visible:
            flatten(root, out, visible)
    return out


def format_line(node):
    indent = "  " * node.depth
    if node.is_dir:
        marker = "▼ " if node.expanded else "▶ "
        label = node.name + "/"
    else:
        marker = "  "
        label = node.name
        if node.is_link:
            try:
                label += " -> " + os.readlink(node.path)
            except OSError:
                pass
    if node.error:
        label += f"  [{node.error}]"
    return indent + marker + label


def set_cursor(visible):
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass  # terminalen kan ikke skjule/vise markøren


def read_escape_sequence(stdscr):
    """Leser resten av en escape-sekvens etter at ESC er mottatt.

    Returnerer tegnene etter ESC som en streng. Tom streng betyr at ESC var
    en vanlig Esc-tast uten noe etter seg.
    """
    stdscr.timeout(30)  # ms å vente på neste tegn
    chars = []
    try:
        while len(chars) < 20:
            try:
                ch = stdscr.get_wch()
            except curses.error:   # ingen flere tegn
                break
            if not isinstance(ch, str):
                break
            chars.append(ch)
            # CSI-sekvenser ender på et tegn i området 0x40-0x7E (etter "[")
            if len(chars) > 1 and 0x40 <= ord(ch) <= 0x7E:
                break
    finally:
        stdscr.timeout(-1)  # tilbake til blokkerende lesing
    return "".join(chars)


def read_key(stdscr):
    """Leser én tast. Returnerer (tast, handling).

    tast er et tegn (str) for vanlig tekst, ellers en heltallskode
    (curses.KEY_*, eller ASCII-koden for kontrolltegn). -1 betyr ignorer.
    handling er "open_one" (Ctrl+pil ned), "close_one" (Ctrl+pil opp) eller None.
    """
    try:
        ch = stdscr.get_wch()
    except curses.error:
        return -1, None

    # Spesialtast (piltaster, mus, ...) – kodene er heltall
    if isinstance(ch, int):
        # Ctrl+pil gjenkjent av ncurses (terminfo-navnene kUP5 / kDN5)
        name = curses.keyname(ch)
        if name == b"kUP5":
            return -1, "close_one"
        if name == b"kDN5":
            return -1, "open_one"
        return ch, None

    if ch == "\x1b":
        seq = read_escape_sequence(stdscr)
        if seq == "":
            return 27, None  # vanlig Esc
        # Ctrl+pil som rå escape-sekvens (reserve hvis ncurses ikke kjenner den)
        if seq in CTRL_UP_SEQS:
            return -1, "close_one"
        if seq in CTRL_DOWN_SEQS:
            return -1, "open_one"
        return -1, None  # ukjent sekvens, ignorer

    if ch in ("\x7f", "\b"):
        return curses.KEY_BACKSPACE, None
    if ch.isprintable():
        return ch, None          # vanlig tegn som skal inn i filterfeltet
    return ord(ch), None         # kontrolltegn (Enter = 10/13, Ctrl+U = 21 ...)


def open_in_vim(stdscr, path):
    """Åpner filen i vim. Curses settes på pause og gjenopprettes når vim avsluttes.

    Returnerer en feilmelding (str) hvis noe gikk galt, ellers en tom streng.
    """
    curses.def_prog_mode()   # husk curses-tilstanden
    curses.endwin()          # gi terminalen tilbake til skallet
    message = ""
    try:
        subprocess.call(["vim", path])
    except FileNotFoundError:
        message = "Fant ikke vim. Er det installert og i PATH?"
    except OSError as e:
        message = f"Kunne ikke starte vim: {e.strerror or e}"
    finally:
        curses.reset_prog_mode()  # gjenopprett curses (inkl. musemodus)
        set_cursor(True)
        stdscr.clearok(True)      # tving full omtegning
        stdscr.refresh()
    return message


def toggle_recursive(node, force_expand=False):
    """Åpne rekursivt, eller lukk rekursivt hvis alt allerede er åpent.

    Returnerer en eventuell statusmelding.
    """
    if node.is_fully_expanded() and not force_expand:
        node.collapse_recursive()
        return ""
    if not node.expand_recursive():
        return f"Stoppet etter {MAX_RECURSIVE_NODES} oppføringer (for stort tre)"
    return ""


def draw(stdscr, nodes, selected, top, message, filt):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    list_height = height - 2  # én linje til filterfelt, én til statuslinje
    bar_attr = curses.color_pair(3) if curses.has_colors() else curses.A_REVERSE

    # Linje 1: filterfelt
    label = " Filter: "
    stdscr.addnstr(0, 0, " " * width, width, bar_attr)
    stdscr.addnstr(0, 0, label, width, bar_attr | curses.A_BOLD)
    cursor_x = len(label)
    avail = width - len(label) - 1
    if avail > 0:
        if filt.text:
            shown = filt.text[-avail:]  # lang tekst: vis slutten
            stdscr.addnstr(0, len(label), shown, avail, bar_attr)
            cursor_x += len(shown)
        else:
            stdscr.addnstr(0, len(label), "skriv for å filtrere stier …", avail,
                           bar_attr | curses.A_DIM)

    # Treet
    for row in range(list_height):
        idx = top + row
        if idx >= len(nodes):
            break
        node = nodes[idx]
        line = format_line(node)

        if node.is_dir:
            attr = curses.color_pair(1) | curses.A_BOLD
        elif node.is_link:
            attr = curses.color_pair(2)
        else:
            attr = curses.A_NORMAL
        # Mapper som bare vises fordi de inneholder treff, tones ned
        if filt.matches is not None and node not in filt.matches:
            attr = (attr & ~curses.A_BOLD) | curses.A_DIM
        if idx == selected:
            attr |= curses.A_REVERSE
            line = line.ljust(width)

        stdscr.addnstr(row + 1, 0, line, width - 1 if idx != selected else width, attr)

    if not nodes and list_height > 1:
        stdscr.addnstr(2, 2, "Ingen treff", max(0, width - 3), curses.A_DIM)

    # Statuslinje
    if message:
        footer = message
    elif filt.active and not filt.complete:
        footer = (f" Søket er begrenset til {MAX_FILTER_NODES} oppføringer"
                  " – listen kan være ufullstendig ")
    else:
        footer = " Enter: åpne/lukk alt  Ctrl+↓/↑: ett nivå  Esc: tøm filter/avslutt "
    pos = f"{selected + 1 if nodes else 0}/{len(nodes)} "
    footer = footer.ljust(max(0, width - len(pos))) + pos
    try:
        stdscr.addnstr(height - 1, 0, footer, width - 1, bar_attr)
    except curses.error:
        pass  # Skriving i nederste høyre hjørne kan gi feil – trygt å ignorere

    # Markøren står i filterfeltet
    stdscr.move(0, min(cursor_x, max(0, width - 1)))
    stdscr.refresh()


def main(stdscr, paths):
    set_cursor(True)  # markøren vises i filterfeltet
    stdscr.keypad(True)
    try:
        curses.set_escdelay(25)  # raskere Esc (Python 3.9+)
    except AttributeError:
        pass

    if curses.has_colors():  # enkelte terminaler (f.eks. vt100) har ikke farger
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_CYAN, bg)                    # kataloger
        curses.init_pair(2, curses.COLOR_MAGENTA, bg)                 # symlenker
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)   # felt/statuslinje

    # Ingen syntetiske klikk: vi vil ha BUTTON1_PRESSED med en gang
    curses.mouseinterval(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)

    btn_up = getattr(curses, "BUTTON4_PRESSED", 0)
    btn_down = getattr(curses, "BUTTON5_PRESSED", 0)

    # Hver oppgitte katalog blir en rot (ekspandert ett nivå ved oppstart)
    roots = []
    for path in paths:
        root = Node(path, label=path)
        root.expand()
        roots.append(root)

    filt = Filter()
    nodes = visible_nodes(roots)
    selected = 0
    top = 0
    message = ""

    def refilter():
        """Kalles når filterteksten er endret."""
        nonlocal selected, top
        filt.apply(roots, on_scan=lambda: draw(
            stdscr, nodes, selected, top, " Leser inn katalogtreet …", filt))
        selected = 0
        top = 0

    while True:
        nodes = visible_nodes(roots, filt.visible)
        height, _ = stdscr.getmaxyx()
        list_height = max(1, height - 2)

        # Hold markeringen innenfor gyldig område og synlig i vinduet
        selected = max(0, min(selected, len(nodes) - 1))
        if selected < top:
            top = selected
        elif selected >= top + list_height:
            top = selected - list_height + 1
        top = max(0, min(top, max(0, len(nodes) - list_height)))

        draw(stdscr, nodes, selected, top, message, filt)
        key, action = read_key(stdscr)
        message = ""  # statusmeldingen vises bare til neste hendelse
        node = nodes[selected] if nodes else None

        # --- Tekst til filterfeltet ---
        if isinstance(key, str):
            filt.text += key
            refilter()
            continue

        if key == curses.KEY_BACKSPACE:
            if filt.text:
                filt.text = filt.text[:-1]
                refilter()
        elif key == KEY_CTRL_U:
            if filt.text:
                filt.text = ""
                refilter()
        elif key == KEY_CTRL_W:
            if filt.text:
                filt.text = filt.text.rstrip()
                filt.text = filt.text[:filt.text.rfind(" ") + 1]
                refilter()

        elif key == 27:
            # Esc: tøm filteret først, avslutt når det allerede er tomt
            if filt.active:
                filt.text = ""
                refilter()
            else:
                break

        elif key == curses.KEY_RESIZE:
            continue

        # --- Ctrl+pil: ett nivå ---
        elif action == "open_one":
            if node:
                node.expand()
        elif action == "close_one":
            if node:
                node.collapse()

        elif key == -1:
            continue

        elif key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, state = curses.getmouse()
            except curses.error:
                continue

            if state & btn_up:
                top = max(0, top - 3)
            elif state & btn_down:
                top = min(max(0, len(nodes) - list_height), top + 3)
            elif state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                # CLICKED: ncurses slår trykk+slipp sammen hvis de kommer samtidig
                if 1 <= my <= list_height:
                    idx = top + (my - 1)
                    if idx < len(nodes):
                        selected = idx
                        clicked = nodes[idx]
                        if clicked.is_dir:
                            if state & curses.BUTTON_CTRL:
                                message = toggle_recursive(clicked)
                            else:
                                clicked.toggle()
                        else:
                            message = open_in_vim(stdscr, clicked.path)

        # --- Navigasjon i listen ---
        elif key == curses.KEY_UP:
            selected -= 1
        elif key == curses.KEY_DOWN:
            selected += 1
        elif key == curses.KEY_PPAGE:
            selected -= list_height
        elif key == curses.KEY_NPAGE:
            selected += list_height
        elif key == curses.KEY_HOME:
            selected = 0
        elif key == curses.KEY_END:
            selected = len(nodes) - 1

        elif key in (curses.KEY_ENTER, 10, 13):
            # Enter: rekursiv åpning/lukking (standard)
            if node:
                if node.is_dir:
                    message = toggle_recursive(node)
                else:
                    message = open_in_vim(stdscr, node.path)

        elif key == curses.KEY_RIGHT:
            if node:
                node.expand()

        elif key == curses.KEY_LEFT:
            if node:
                if node.is_dir and node.expanded:
                    node.collapse()
                elif node.parent is not None and node.parent in nodes:
                    # Hopp til forelderkatalogen
                    selected = nodes.index(node.parent)


if __name__ == "__main__":
    try:
        locale.setlocale(locale.LC_ALL, "")  # nødvendig for æøå i tekstfeltet
    except locale.Error:
        pass

    args = sys.argv[1:] or ["."]

    paths = []
    for arg in args:
        full = os.path.abspath(arg)
        if not os.path.isdir(full):
            sys.exit(f"Ikke en katalog: {arg}")
        if full not in paths:  # ignorer duplikater
            paths.append(full)

    try:
        curses.wrapper(main, paths)
    except KeyboardInterrupt:
        pass
