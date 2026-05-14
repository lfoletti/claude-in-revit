#! python3
# -*- coding: utf-8 -*-
"""Single conversational entry point for claude-in-revit.

Flow per click (each click = fresh CPython process, so all state lives on disk):

  1. Resolve `doc` from pyRevit, gate on null / unsaved projects.
  2. `kg_sync.open_or_create(doc)` — load the project's KG cache or scaffold
     an empty one. Refuse if the KG hasn't been bootstrapped yet (point user
     at the Refresh KG button to seed levels + wall types from Revit).
  3. Load the Anthropic conversation history from
     `~/.config/claude-in-revit/projects/<id>.history.json`.
  4. Pop up a `Microsoft.VisualBasic.Interaction.InputBox` for the user
     prompt (single-line — WPF custom form is a UX upgrade for later).
  5. `LLMClient.run_turn(..., doc=doc)` — the dispatcher injects `doc` into
     Revit-aware tools (walls_create etc.), so mutations land in Revit + KG
     atomically.
  6. Persist the updated history; surface the LLM text + tools used + tokens
     in a TaskDialog.

Defensive shell (BaseException + traceback) inherited from the Phase 6
pattern — surfaces any failure as a readable TaskDialog instead of Revit's
generic "External command failed" NRE.
"""
__title__ = "Prompt"
__doc__ = "Talk to the LLM agent (single conversational entry point)."

import os
import sys
import traceback

# pyRevit puts `<extension>/lib/` on sys.path but not the extension root.
_EXT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _EXT_ROOT not in sys.path:
    sys.path.insert(0, _EXT_ROOT)

import clr  # PythonNet — pre-loaded by pyRevit's CPython engine.
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

from Autodesk.Revit.UI import TaskDialog
from System.Drawing import Point, Size
from System.Windows.Forms import (
    AnchorStyles,
    Button,
    CheckBox,
    DialogResult,
    Form,
    FormBorderStyle,
    FormStartPosition,
    Keys,
    Label,
    MessageBox,
    MessageBoxButtons,
    MessageBoxIcon,
    OpenFileDialog,
    ScrollBars,
    TextBox,
)


def _show_error(title, body):
    """TaskDialog brut pour les chemins d'erreur — pas de dépendance
    autre que Autodesk.Revit.UI, donc survit même si `lib.*` casse."""
    TaskDialog.Show("claude-in-revit — {}".format(title), body)


def _show(title, body):
    """Dialog sélectionnable + bouton "Copier" pour les outputs normaux.
    Fallback TaskDialog si `lib.ui_dialogs` n'est pas importable."""
    try:
        from lib.ui_dialogs import show_selectable_text
        show_selectable_text("claude-in-revit — {}".format(title), body)
    except Exception:  # noqa: BLE001
        _show_error(title, body)


_MAX_ATTACHMENT_BYTES = 1 * 1024 * 1024  # mirror lib.llm_api.MAX_ATTACHMENT_BYTES.
_ATTACHMENT_FILTER = (
    "Tous fichiers supportés (*.png;*.jpg;*.jpeg;*.gif;*.webp;*.pdf;"
    "*.txt;*.md;*.csv;*.json;*.py;*.log;*.html;*.xml;*.yaml;*.yml;*.ini;"
    "*.cfg;*.toml;*.tsv;*.rst)|*.png;*.jpg;*.jpeg;*.gif;*.webp;*.pdf;"
    "*.txt;*.md;*.csv;*.json;*.py;*.log;*.html;*.xml;*.yaml;*.yml;*.ini;"
    "*.cfg;*.toml;*.tsv;*.rst|"
    "Tous fichiers (*.*)|*.*"
)


def _ask(prompt_text, title, default=""):
    """Modal prompt input with optional file attachment + reset checkbox.

    Returns `(text, attachment_path or None, reset_history_bool)`.
    `("", None, False)` on cancel.

    Layout :
      - Question label en haut.
      - TextBox 5 lignes (multi-line, scroll vertical) sous la question.
      - Ligne "Joindre un fichier...": bouton + label de statut.
      - Checkbox "Réinitialiser la conversation".
      - OK / Annuler en bas à droite.

    Clavier : Enter = newline ; Ctrl+Enter = submit ; Esc = cancel.
    """
    import os

    form = Form()
    form.Text = title
    form.ClientSize = Size(500, 280)
    form.FormBorderStyle = FormBorderStyle.FixedDialog
    form.MaximizeBox = False
    form.MinimizeBox = False
    form.StartPosition = FormStartPosition.CenterScreen

    label = Label()
    label.Text = prompt_text
    label.AutoSize = False
    label.Location = Point(12, 12)
    label.Size = Size(476, 24)
    form.Controls.Add(label)

    textbox = TextBox()
    textbox.Multiline = True
    textbox.AcceptsReturn = True
    textbox.AcceptsTab = False
    textbox.ScrollBars = ScrollBars.Vertical
    textbox.Location = Point(12, 44)
    textbox.Size = Size(476, 100)
    textbox.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
    if default:
        textbox.Text = default
    form.Controls.Add(textbox)

    # Attachment row.
    attachment_state = {"path": None}

    attach_btn = Button()
    attach_btn.Text = "Joindre un fichier..."
    attach_btn.Size = Size(150, 28)
    attach_btn.Location = Point(12, 156)
    attach_btn.Anchor = AnchorStyles.Bottom | AnchorStyles.Left
    form.Controls.Add(attach_btn)

    attach_label = Label()
    attach_label.Text = "(aucun fichier joint)"
    attach_label.AutoSize = False
    attach_label.Location = Point(170, 162)
    attach_label.Size = Size(318, 22)
    attach_label.Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right
    form.Controls.Add(attach_label)

    clear_btn = Button()
    clear_btn.Text = "×"
    clear_btn.Size = Size(28, 28)
    clear_btn.Location = Point(460, 156)
    clear_btn.Anchor = AnchorStyles.Bottom | AnchorStyles.Right
    clear_btn.Visible = False
    form.Controls.Add(clear_btn)

    def _on_attach_click(sender, e):
        dlg = OpenFileDialog()
        dlg.Title = "Joindre un fichier au prompt"
        dlg.Filter = _ATTACHMENT_FILTER
        dlg.CheckFileExists = True
        dlg.Multiselect = False
        if dlg.ShowDialog() != DialogResult.OK:
            return
        path = dlg.FileName
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            MessageBox.Show(
                "Impossible de lire le fichier : {}".format(exc),
                "Erreur",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error,
            )
            return
        if size > _MAX_ATTACHMENT_BYTES:
            MessageBox.Show(
                "Fichier trop volumineux : {:.2f} MB (limite 1 MB).".format(
                    size / (1024 * 1024),
                ),
                "Fichier refusé",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning,
            )
            return
        attachment_state["path"] = path
        attach_label.Text = "{} ({:.1f} KB)".format(
            os.path.basename(path), size / 1024,
        )
        clear_btn.Visible = True

    def _on_clear_click(sender, e):
        attachment_state["path"] = None
        attach_label.Text = "(aucun fichier joint)"
        clear_btn.Visible = False

    attach_btn.Click += _on_attach_click
    clear_btn.Click += _on_clear_click

    # Reset-history checkbox.
    reset_cb = CheckBox()
    reset_cb.Text = "Réinitialiser la conversation"
    reset_cb.AutoSize = True
    reset_cb.Location = Point(12, 196)
    reset_cb.Anchor = AnchorStyles.Bottom | AnchorStyles.Left
    form.Controls.Add(reset_cb)

    # Bottom-right OK / Cancel.
    ok = Button()
    ok.Text = "OK"
    ok.Size = Size(80, 28)
    ok.Location = Point(326, 228)
    ok.DialogResult = DialogResult.OK
    ok.Anchor = AnchorStyles.Bottom | AnchorStyles.Right
    form.Controls.Add(ok)

    cancel = Button()
    cancel.Text = "Annuler"
    cancel.Size = Size(80, 28)
    cancel.Location = Point(412, 228)
    cancel.DialogResult = DialogResult.Cancel
    cancel.Anchor = AnchorStyles.Bottom | AnchorStyles.Right
    form.Controls.Add(cancel)

    form.CancelButton = cancel

    def _on_keydown(sender, e):
        if e.Control and e.KeyCode == Keys.Enter:
            form.DialogResult = DialogResult.OK
            form.Close()
            e.Handled = True
            e.SuppressKeyPress = True

    textbox.KeyDown += _on_keydown
    textbox.Select()

    if form.ShowDialog() == DialogResult.OK:
        return textbox.Text, attachment_state["path"], bool(reset_cb.Checked)
    return "", None, False


def _format_selection_line(selection_ids, unbound_by_category, refresh_actionable):
    """One-line summary of the active Revit selection, for the system prompt.

    - Empty selection → "Sélection active : aucune".
    - Only mapped elements → "2 mappé(s) — wall_001, wall_003".
    - Mix → "2 mappé(s) — wall_001, wall_003 ; non mappé(s) : 1 Murs, 1 Lignes
      de détail". The "lance Refresh KG" suggestion only appears when
      `refresh_actionable` is True — typically when an unbound element
      is in a category covered by `full_rescan` (Walls). For pure
      annotation selections (lines, dimensions, text notes), suggesting
      a refresh would be misleading.
    """
    if not selection_ids and not unbound_by_category:
        return "Sélection active : aucune"
    parts = []
    if selection_ids:
        parts.append("{} mappé(s) — {}".format(
            len(selection_ids), ", ".join(selection_ids),
        ))
    if unbound_by_category:
        unbound_total = sum(unbound_by_category.values())
        cats = ", ".join(
            "{} {}".format(count, name)
            for name, count in sorted(unbound_by_category.items())
        )
        suffix = ""
        if refresh_actionable:
            suffix = " (au moins un dans une catégorie modélisée — un Refresh KG les ferait apparaître)"
        parts.append("{} non mappé(s) : {}{}".format(unbound_total, cats, suffix))
    return "Sélection active : " + " ; ".join(parts)


_STATIC_SYSTEM_PROMPT = (
    "Tu es claude-in-revit, un agent intégré à Autodesk Revit.\n\n"
    "Tu opères sur un projet via un Knowledge Graph (KG) synchronisé "
    "atomiquement avec Revit : toute création / modification que tu "
    "demandes via les tools s'applique au modèle BIM et au KG dans "
    "la même transaction.\n\n"
    "Conventions :\n"
    "- Coordonnées 2D (x, y) en mètres dans le plan du niveau.\n"
    "- Hauteurs et épaisseurs en mètres.\n"
    "- llm_ids stables (`level_001`, `wall_003`) — utilise-les comme "
    "  refs entre tools.\n"
    "- Avant de créer des murs, appelle `catalog_list_levels` et "
    "  `catalog_list_wall_types` pour découvrir les références.\n"
    "- **Résolution des noms de niveaux** : quand l'utilisateur cite un "
    "  niveau par son nom (« SS01 », « Niveau 1 », « rez », « toiture »), "
    "  **TOUJOURS** appeler `catalog_list_levels` *au début du tour* pour "
    "  mapper nom → llm_id, **avant** d'agir. Ne jamais deviner un llm_id "
    "  de niveau depuis l'historique conversationnel : ces refs peuvent "
    "  avoir changé entre tours (rescan, soft-delete, etc.). Si plusieurs "
    "  niveaux ont des noms ambigus pour le contexte, demande à "
    "  l'utilisateur de préciser plutôt que choisir au hasard.\n"
    "- **Flags `preserve_*` des setters d'openings (`preserve_sill`, "
    "  `preserve_head`)** : NE LES PASSE JAMAIS. Le défaut `True` est "
    "  la SEULE valeur correcte pour 99% des cas (auto-découple sill↔head "
    "  via création de variant). Tu ne dois passer `preserve_*=False` "
    "  que si l'utilisateur **explicitement** demande de laisser dériver "
    "  l'autre dimension (phrase type : « accepte que l'allège bouge » ou "
    "  « ne crée pas de variant »). Sans cette demande explicite, omettre "
    "  le flag entièrement et faire confiance au défaut. Mauvaise "
    "  manipulation du flag = bug rapporté 2026-05-12 : sill recomputé "
    "  par Revit alors que l'utilisateur voulait le préserver.\n"
    "- Si la requête utilise des démonstratifs (« ce mur », « ces », "
    "  « this/these ») ou des pronoms implicites (« supprime-le », "
    "  « déplace-les »), prends les llm_ids de la *sélection active* "
    "  comme cibles par défaut sans redemander de précision.\n"
    "- **Création en masse : préfère les tools de pattern et les tools "
    "  `_many` quand tu as plusieurs éléments à créer en une fois.**\n"
    "  Ordre de préférence (du plus économe au moins économe en tokens) :\n"
    "    1. **Pattern tool** (ex. `columns_create_grid`, "
    "       `elements_array_parametric`) — pour les agencements "
    "       paramétriques. Tu passes les paramètres, Python calcule.\n"
    "    2. **Bulk `_many`** quand le pattern n'a pas de tool dédié.\n"
    "    3. **Unitaire** uniquement pour 1-2 éléments isolés.\n"
    "  Au-delà de ~5 créations, **ne JAMAIS** utiliser le tool unitaire "
    "  en boucle.\n"
    "- **Identifiants après création : ne JAMAIS deviner un llm_id "
    "  par numérotation séquentielle, offset ou calcul.** Le seul "
    "  llm_id valide est celui retourné dans le `tool_result`. Si tu "
    "  enchaînes plusieurs créations puis tu dois modifier, appelle "
    "  `catalog_list_<type>` ou `query_get_node` **avant** la modif "
    "  — les compteurs llm_id peuvent avoir des trous (suppressions, "
    "  sessions antérieures), donc ids non contigus.\n"
    "- **Quantificateurs universels (« tous », « toutes », « chaque », "
    "  « l'ensemble des », « la totalité des », « all/every/each ») : "
    "  le pushbutton détecte ces tournures et injecte automatiquement "
    "  un bloc `<auto_scan_kg>` en tête du message utilisateur avec "
    "  le résultat à jour du `catalog_list_<type>` correspondant.** "
    "  Quand tu vois un bloc `<auto_scan_kg>`, c'est l'énumération "
    "  exhaustive faisant autorité — itère sur ces llm_ids directement, "
    "  ne RAPPELLE PAS `catalog_list_*` pour les collections déjà "
    "  listées dedans, et ne te fie JAMAIS à ta mémoire conversationnelle "
    "  pour cette collection-là. Si l'expression universelle vise une "
    "  collection qui n'est PAS dans l'autoscan (cas non couvert par "
    "  le préprocesseur), appelle `catalog_list_<type>` toi-même avant "
    "  d'agir.\n"
    "- **Sur erreur de tool**, ne ré-essaye pas la même commande à "
    "  l'identique. Lis le message d'erreur, ajuste les arguments "
    "  ou bien remonte le blocage à l'utilisateur en clair plutôt "
    "  que de griller des tokens en tentatives répétées.\n"
    "- **Confirmation utilisateur uniquement en cas de doute, pas "
    "  systématiquement.** Quand un tool retourne une inférence avec "
    "  un flag de confiance haute (`all_inferred_confidently=True`, "
    "  `inferred_view_dir` non-None, etc.), procède directement sans "
    "  demander confirmation. Les dialogs `ui_confirm_*` et les "
    "  questions conversationnelles sont réservées aux ambiguïtés "
    "  réelles : inférence impossible, conflit entre sources, action "
    "  destructrice, edge case hors-domaine de confiance. Le "
    "  sur-prompting casse la fluidité — fais confiance aux données "
    "  quand elles sont claires.\n"
    "- **Exception : les niveaux exigent TOUJOURS une validation "
    "  utilisateur, mais GROUPÉE en un seul dialog.** Après "
    "  `levels_reconcile_with_dxf`, ouvre UN SEUL `ui_confirm_choices` "
    "  (ou `ui_confirm_yes_no` si `alignment_complete=True`) qui "
    "  présente l'ensemble des actions proposées via le champ "
    "  `summary_for_dialog`. Jamais un dialog par niveau ou par "
    "  action. Cascade sur les hôtes (murs, ouvertures, sols) =  "
    "  raison de garder le user dans la boucle même quand l'inférence "
    "  est sûre.\n"
    "- **Matching coupe DXF ↔ trait de coupe : utilise "
    "  `dxf_assign_coupes_to_traits`, pas l'ordre des fichiers.** "
    "  `dwg_find_section_markers` retourne les traits triés par "
    "  longueur. L'ordre des fichiers `Coupe 1.dxf`, `Coupe 2.dxf` "
    "  ne matche PAS l'ordre des markers en général — risque de "
    "  swap (la grande coupe assignée au petit trait). Toujours "
    "  appeler `dxf_assign_coupes_to_traits(coupe_paths, markers)` "
    "  pour récupérer l'assignment optimal par drift minimum, et "
    "  l'utiliser pour `dxf_context_register_section_line` + "
    "  `views_link_cad`.\n"
    "- **IMPORT PROJET — pipeline Phase 1 → Phase 2 dans le même tour.** "
    "  Pour les prompts d'import projet (« importe ce projet », "
    "  « inspecte ce dossier de DXF », etc.), tu enchaînes les deux "
    "  phases sans demander à l'utilisateur de relancer un prompt. "
    "  **Gate obligatoire** : appelle `check_planset_integrity(directory)` "
    "  en tout premier. Selon `gate_status` :\n"
    "    • `abort` (severity=errors) → STOPPE immédiatement, présente "
    "      les `errors` à l'user pour résolution (export DXF à corriger, "
    "      etc.). Pas de Phase 1 ni Phase 2 tant que non résolu.\n"
    "    • `needs_user` (severity=warnings) → présente les `warnings` "
    "      via un seul `ui_confirm_yes_no` (« continuer malgré les "
    "      avertissements ? »). Si l'user confirme, enchaîne Phase 1 "
    "      puis Phase 2. Sinon, stoppe et résume les warnings.\n"
    "    • `pass` (severity=clean) → enchaîne directement Phase 1 puis "
    "      Phase 2 sans confirmation supplémentaire.\n"
    "  **Phase 1 (setup)** : (a) `dwg_inspect_sections`, (b) traits de "
    "  coupe via `dwg_find_section_markers` + `dxf_assign_coupes_to_traits`, "
    "  (c) `levels_reconcile_with_dxf` → si actions, **GROUPE en un seul "
    "  `ui_confirm_choices`** (exception niveaux), (d) "
    "  `views_create_section_many`, (e) link **TOUS les DXF** "
    "  (plan/coupes/élévations) via `views_link_cad_many` + "
    "  `dxf_context_register_linked_view_many`. Les élévations DXF "
    "  (`kind='elevation'`, `direction` ∈ {Est, Nord, Sud, Ouest}) se "
    "  linkent dans les vues élévations Revit (via "
    "  `catalog_list_elevation_views`). Le plan DXF se linke dans la "
    "  vue Plan d'étage du Niveau correspondant.\n"
    "  **Phase 2 (création BIM) — enchaîne 2a, 2b, 2c dans le même tour** :\n"
    "  **Phase 2a — murs continus** : (1) "
    "  `dwg_extract_wall_thicknesses_many` (info dans le résumé), "
    "  (2) `dwg_create_continuous_walls_many` avec `items=[{file_path, "
    "  level_ref, height_m}, …]`. Fusionne les fragments mur via vote "
    "  élévation. Suspects flagués (score 3D < 2) signalés dans le "
    "  résumé pour suppression manuelle user post-import.\n"
    "  **Phase 2b — openings** : (3) `dwg_add_openings_to_walls_many` "
    "  énumère les fenêtres depuis le **plan** (source primaire pour "
    "  nombre+position), enrichit width depuis plan, height depuis "
    "  élévation, sill depuis coupe. Vote orientation, crée FamilyType "
    "  `DXF_WIN_WxH` / `DXF_DOOR_WxH`. Skip pre-création les fenêtres "
    "  trop larges pour leur mur hôte (évite erreur Revit 'ne coupent rien').\n"
    "  **Phase 2c — sols** : (4) `dwg_create_floors_many` dérive "
    "  l'épaisseur des dalles depuis les paires A-FLOR horizontales des "
    "  coupes, construit le boundary depuis le convex hull des murs par "
    "  niveau, crée `DXF_FLOOR_<cm>cm` et les Floor instances. Skip la "
    "  toiture par défaut.\n"
    "  **Fin** : (5) `views_open_3d` pour validation visuelle, puis "
    "  résumé consolidé (Phase 1 + Phase 2a/b/c : fichiers, niveaux, "
    "  types créés, murs/fenêtres/sols importés par niveau).\n"
    "  **Bulks obligatoires** : `views_create_section_many`, "
    "  `views_link_cad_many`, `dxf_context_register_section_line_many`, "
    "  `dxf_context_register_linked_view_many`, "
    "  `dwg_extract_wall_thicknesses_many`, `dwg_import_walls_typed_many`. "
    "  Jamais les unitaires en boucle pour ces flows.\n\n"
    "Réponds dans la langue de l'utilisateur."
)


def _dynamic_state_block(kg, selection_ids, unbound_by_category, refresh_actionable):
    """Per-turn state (project_id, turn counter, KG counts, selection).

    Returned as the second system block (no cache_control) so the
    static instructions above stay in the cached prefix turn after
    turn, while this state is re-encoded fresh.
    """
    # FamilyType nodes are shared across hosted families (Door, Window,
    # eventually Furniture, …) — discriminated by `category`. Count
    # them per-category so the state block reflects the actual room
    # the LLM has to maneuver in.
    door_types = 0
    window_types = 0
    for nid in kg.find_by_type("FamilyType"):
        cat = kg.get_node(nid).get("category")
        if cat == "Doors":
            door_types += 1
        elif cat == "Windows":
            window_types += 1
    return (
        "## État courant du projet\n\n"
        "Project ID : {project_id}\n"
        "Turn courant : {turn}\n"
        "Niveaux : {levels} | Types de mur : {wall_types} | "
        "Murs : {walls} | Lignes modèle : {model_lines} | "
        "Lignes détail : {detail_lines} | Types de poteau : "
        "{column_types} | Poteaux : {columns} | Types de porte : "
        "{door_types} | Portes : {doors} | Types de fenêtre : "
        "{window_types} | Fenêtres : {windows}\n"
        "{selection_line}"
    ).format(
        project_id=kg.project_id,
        turn=kg.turn,
        levels=kg.count_by_type("Level"),
        wall_types=kg.count_by_type("WallType"),
        walls=kg.count_by_type("Wall"),
        model_lines=kg.count_by_type("ModelLine"),
        detail_lines=kg.count_by_type("DetailLine"),
        column_types=kg.count_by_type("ColumnType"),
        columns=kg.count_by_type("Column"),
        door_types=door_types,
        doors=kg.count_by_type("Door"),
        window_types=window_types,
        windows=kg.count_by_type("Window"),
        selection_line=_format_selection_line(
            selection_ids, unbound_by_category, refresh_actionable,
        ),
    )


def _system_blocks(kg, selection_ids, unbound_by_category, refresh_actionable):
    """Two system blocks : STATIC (cached) + DYNAMIC (per-turn).

    Anthropic's prompt cache breakpoint sits at the *end* of the block
    carrying `cache_control: ephemeral`. By making block 0 the stable
    instructions and block 1 the per-turn KG state, the cache prefix
    is consistent across turns → `cache_read` instead of `cache_write`
    after the first turn. Saves ~10 K tokens/turn once the model is
    above its cache threshold (Sonnet 4.6 = 2048 tokens).
    """
    return [
        {
            "type": "text",
            "text": _STATIC_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": _dynamic_state_block(
                kg, selection_ids, unbound_by_category, refresh_actionable,
            ),
        },
    ]


def _fmt_usage(u, stop_reason):
    return (
        "Tokens — in={} out={} cache_read={} cache_write={} | "
        "api_calls={} | stop={}".format(
            u.input_tokens,
            u.output_tokens,
            u.cache_read_input_tokens,
            u.cache_creation_input_tokens,
            u.api_calls,
            stop_reason,
        )
    )


def _main():
    from lib import config, kg_sync, preprocess
    from lib.llm_api import (
        LLMClient,
        build_user_content,
        load_history,
        sanitize_history,
        save_history,
        trim_history_to_max_chars,
    )

    # Bare-name __revit__ — globals().get() doesn't see CPython injections
    # (cf. CLAUDE.md "gotchas CPython" and JOURNAL.md Phase 7 session 2).
    try:
        uiapp = __revit__  # type: ignore[name-defined]
    except NameError:
        try:
            from pyrevit import HOST_APP
            uiapp = HOST_APP.uiapp
        except Exception as fallback_exc:  # noqa: BLE001
            raise RuntimeError(
                "__revit__ not in scope and pyrevit.HOST_APP fallback "
                "failed ({})".format(fallback_exc)
            )

    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        _show_error("Prompt", "Aucun document Revit actif. Ouvre un projet, puis recommence.")
        return

    doc = uidoc.Document
    if doc is None:
        raise RuntimeError("ActiveUIDocument has no Document.")

    path_name = (getattr(doc, "PathName", "") or "").strip()
    if not path_name:
        _show_error(
            "Prompt",
            "Le projet Revit n'est pas encore sauvegardé.\n\n"
            "Enregistre-le d'abord (Fichier → Enregistrer sous), puis "
            "recommence. L'identifiant du KG est dérivé du chemin du .rvt "
            "(§8 du DESIGN doc).",
        )
        return

    kg = kg_sync.open_or_create(doc)

    # Auto-sync : consume any pending DocumentChanged events queued by
    # the pyRevit hook `hooks/doc-changed.py`. No-op if mode is MANUEL
    # (sentinel present) or if the buffer is empty. Runs *before* the
    # catalog-empty check so the LLM sees a fresh KG.
    diff_summary = kg_sync.consume_pending_diffs(kg, doc)

    # Heuristic: a fresh KG with no Levels means the user hasn't pressed
    # Refresh KG yet. Without it, the LLM sees an empty catalogue and
    # walls_create has no refs to use. Point them at the right button
    # rather than letting them spend tokens on a dead-end conversation.
    if not kg.find_by_type("Level") and not kg.find_by_type("WallType"):
        _show_error(
            "Prompt",
            "Le KG du projet est vide.\n\n"
            "Clique d'abord sur « Refresh KG » pour synchroniser le KG "
            "avec ton modèle Revit, puis recommence ici.",
        )
        return

    history_path = config.history_path_for(kg.project_id)
    history = load_history(history_path)
    # Defend against a corrupted history (e.g. a previous session crashed
    # mid-tool-use loop, leaving an assistant turn with `tool_use` blocks
    # but no matching `tool_result` blocks — Anthropic rejects that with
    # `invalid_request_error`). Truncate to the longest valid prefix.
    history, dropped_sanitize = sanitize_history(history)
    # Cap the working window — past a threshold, older turns are
    # dropped from the front. Without this, a long session sees
    # input-token cost grow linearly turn after turn (every previous
    # turn is re-sent each round).
    history, dropped_trim = trim_history_to_max_chars(history, max_chars=120_000)
    dropped_history_entries = dropped_sanitize + dropped_trim
    if dropped_history_entries:
        save_history(history, history_path)

    # Snapshot the user's Revit selection *before* opening the modal —
    # opening a Form may steal focus and let Revit clear the selection.
    # `doc` passes so unmapped elements get categorised (e.g. annotation
    # lines vs walls) and we can nuance the Refresh KG suggestion.
    selection_ids, unbound_by_category, refresh_actionable = (
        kg_sync.active_selection_llm_ids(uidoc, kg, doc=doc)
    )

    user_prompt, attachment_path, reset_requested = _ask(
        "Que veux-tu faire ?",
        "claude-in-revit",
    )
    if not user_prompt or not user_prompt.strip():
        return
    user_prompt = user_prompt.strip()

    if reset_requested:
        # User asked to flush — start the next turn with a clean
        # conversation. The KG is untouched (project state survives).
        history = []
        save_history(history, history_path)

    # Deterministic safety net: if the user prompt contains an
    # exhaustive quantifier (« toutes les fenêtres », « tous les murs »,
    # « chaque porte », « all the windows », …), pre-scan the relevant
    # KG catalog(s) and inject the result as an `<auto_scan_kg>` block
    # at the top of the user message. This converts the advisory
    # system-prompt rule into a runtime guarantee — the LLM literally
    # sees the live collection state in its context and can't fall
    # back on a stale conversation memory. Token cost paid only when
    # an exhaustive expression actually appears.
    autoscan_preamble = preprocess.autoscan_payload(user_prompt, kg)
    if autoscan_preamble:
        user_prompt = autoscan_preamble + user_prompt

    # Routing tier-2 : détecte les keywords de domaines tier-2 (DWG/DXF
    # en V0) et monte `tier_max` à 2 → le LLM voit les tools concernés.
    # Sans ça, les `dwg_*` étaient invisibles depuis qu'ils ont été
    # livrés (cf. session h, dette routing non implémentée).
    tier_max = preprocess.infer_tier_max(user_prompt)

    # build_user_content wraps prompt + optional attachment into the shape
    # Anthropic expects in `messages=[{"role": "user", "content": …}]`: a
    # bare string when no attachment, a list of content blocks (text +
    # image/document/text-of-file) when one is provided. The file rides
    # along with the prompt in the same HTTPS request (no separate upload
    # endpoint). Refused attachments (>1 MB, unsupported type) raise
    # ValueError which the outer try/except surfaces in a TaskDialog.
    user_content = build_user_content(user_prompt, attachment_path)

    kg.advance_turn()
    client = LLMClient()
    result = client.run_turn(
        kg=kg,
        user_prompt=user_content,
        system_prompt=_system_blocks(
            kg, selection_ids, unbound_by_category, refresh_actionable,
        ),
        history=history,
        tier_max=tier_max,
        doc=doc,
    )
    save_history(history, history_path)

    parts = []
    if result.text:
        parts.append(result.text)
    if result.tool_calls:
        parts.append("\nTools utilisés : " + ", ".join(t["name"] for t in result.tool_calls))
    # Surface the auto-sync activity only when it actually moved something
    # — silence on no-op (empty buffer or all-unbound) to keep the UI quiet.
    if diff_summary.get("modified_applied") or diff_summary.get("deleted_applied"):
        parts.append(
            "\n[KG auto-sync : {} node(s) rafraîchi(s), {} soft-delete(s) "
            "depuis {} event(s) hook]".format(
                diff_summary["modified_applied"],
                diff_summary["deleted_applied"],
                diff_summary["records"],
            )
        )
    if diff_summary.get("skipped_added"):
        parts.append(
            "\n[KG auto-sync : {} élément(s) Revit créé(s) hors agent "
            "ignoré(s) — clique Refresh KG pour les ingérer.]".format(
                diff_summary["skipped_added"],
            )
        )
    if reset_requested:
        parts.append("\n[Historique conversation : réinitialisé sur demande.]")
    elif dropped_history_entries:
        parts.append(
            "\n[Historique conversation : {} entrée(s) tronquée(s) au "
            "chargement (corrompue, ou budget de tokens dépassé). "
            "Le contexte des tours les plus anciens est perdu.]".format(
                dropped_history_entries,
            )
        )
    parts.append("\n" + _fmt_usage(result.usage, result.stop_reason))
    _show("Prompt — réponse", "\n".join(parts))


try:
    _main()
except BaseException as exc:  # noqa: BLE001 — surface everything to the UI.
    # Diagnostic ciblé pour `overloaded_error` Anthropic 529 (serveurs
    # saturés). Différent du crash code Python — le user doit savoir
    # que c'est un problème serveur Anthropic transient, pas un bug
    # de l'agent. Message court + suggestion de retry.
    status_code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None) or {}
    error_obj = body.get("error", {}) if isinstance(body, dict) else {}
    error_type = error_obj.get("type") if isinstance(error_obj, dict) else None

    if status_code == 529 or error_type == "overloaded_error":
        _show_error(
            "Anthropic saturé — réessayer",
            "Les serveurs Anthropic sont en surcharge (`overloaded_error` "
            "529) malgré les retries automatiques (~3 min cumulés). "
            "C'est un pic côté Anthropic, pas un bug de l'agent.\n\n"
            "L'historique de conversation n'a PAS été modifié (le tour "
            "n'a pas eu lieu). Tu peux relancer le pushbutton dans "
            "quelques minutes — le pic se résorbera.\n\n"
            "Statut Anthropic en direct : https://status.anthropic.com",
        )
    else:
        _show_error(
            "Prompt failed",
            "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()),
        )
