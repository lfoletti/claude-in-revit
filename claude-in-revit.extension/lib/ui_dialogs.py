"""ui_dialogs.py — fenêtres modales WinForms pour les pushbuttons.

`pyrevit.forms` étant IronPython-only (cf. CLAUDE.md §gotchas CPython),
on construit les dialogs directement via PythonNet + System.Windows.Forms.

`TaskDialog` Revit reste utilisé pour les erreurs critiques au démarrage
du pushbutton (avant que ce module ne soit importable), parce qu'il
n'a pas de dépendance autre que `Autodesk.Revit.UI`.

**`show_selectable_text(title, body)`** — fenêtre avec TextBox read-only
multi-ligne sélectionnable + bouton "Copier" qui pousse le contenu au
presse-papier, + bouton "Fermer". Police monospace pour rendre les
résumés tabulaires lisibles.
"""
from __future__ import annotations

import clr  # PythonNet — pre-loaded par le runtime pyRevit.

clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

from System.Drawing import Font, FontFamily, FontStyle, Point, Size  # noqa: E402
from System.Windows.Forms import (  # noqa: E402
    AnchorStyles,
    Application,
    BorderStyle,
    Button,
    Clipboard,
    DialogResult,
    Form,
    FormBorderStyle,
    FormStartPosition,
    Keys,
    ScrollBars,
    TextBox,
)


def _monospace_font(size_pt: float = 9.5) -> Font:
    """Renvoie une `Font` Consolas (Windows) ; fallback générique monospace.

    Consolas est livré avec Windows depuis Vista — fiable. Si jamais
    indisponible (poste très atypique), `GenericMonospace` capture
    Courier New par défaut.
    """
    try:
        return Font(FontFamily("Consolas"), size_pt, FontStyle.Regular)
    except Exception:  # noqa: BLE001
        return Font(FontFamily.GenericMonospace, size_pt, FontStyle.Regular)


def show_selectable_text(
    title: str,
    body: str,
    *,
    width: int = 640,
    height: int = 480,
    auto_copy: bool = False,
) -> None:
    """Modal window avec TextBox sélectionnable et bouton "Copier".

    Args:
        title: titre de la fenêtre.
        body: contenu texte (peut contenir des sauts de ligne).
        width: largeur initiale (la fenêtre est redimensionnable).
        height: hauteur initiale.
        auto_copy: si True, pousse le `body` au clipboard dès l'ouverture
            et le bouton affiche "Copié ✓".

    Comportement clavier :
        - Ctrl+A : tout sélectionner dans le TextBox.
        - Ctrl+C : copier la sélection (comportement TextBox natif).
        - Esc : ferme la fenêtre.

    Préserve l'historique du clipboard utilisateur si "Copier" n'est
    pas pressé — pas d'auto-copy par défaut (overridable via flag).
    """
    form = Form()
    form.Text = title
    form.ClientSize = Size(width, height)
    form.FormBorderStyle = FormBorderStyle.Sizable
    form.MaximizeBox = True
    form.MinimizeBox = False
    form.StartPosition = FormStartPosition.CenterScreen
    form.MinimumSize = Size(360, 240)

    textbox = TextBox()
    textbox.Multiline = True
    textbox.ReadOnly = True
    textbox.AcceptsReturn = False
    textbox.AcceptsTab = False
    textbox.ScrollBars = ScrollBars.Vertical
    textbox.WordWrap = True
    textbox.BorderStyle = BorderStyle.FixedSingle
    textbox.Font = _monospace_font()
    textbox.Location = Point(12, 12)
    textbox.Size = Size(width - 24, height - 64)
    textbox.Anchor = (
        AnchorStyles.Top
        | AnchorStyles.Left
        | AnchorStyles.Right
        | AnchorStyles.Bottom
    )
    textbox.Text = body or ""
    form.Controls.Add(textbox)

    copy_btn = Button()
    copy_btn.Text = "Copier"
    copy_btn.Size = Size(100, 28)
    copy_btn.Location = Point(width - 220, height - 40)
    copy_btn.Anchor = AnchorStyles.Bottom | AnchorStyles.Right
    form.Controls.Add(copy_btn)

    close_btn = Button()
    close_btn.Text = "Fermer"
    close_btn.Size = Size(100, 28)
    close_btn.Location = Point(width - 112, height - 40)
    close_btn.Anchor = AnchorStyles.Bottom | AnchorStyles.Right
    close_btn.DialogResult = DialogResult.OK
    form.Controls.Add(close_btn)
    form.CancelButton = close_btn   # Esc → Close.
    form.AcceptButton = close_btn   # Enter → Close (déjà focus textbox).

    def _copy_now():
        """Pousse `body` au clipboard et feedback visuel sur le bouton.

        `Clipboard.SetText` jette `ArgumentNullException` sur string vide ;
        on protège. Le timer-based feedback est trivialisé : on flip le
        text du bouton, l'utilisateur le voit jusqu'au prochain focus
        change ou close.
        """
        try:
            if body:
                Clipboard.SetText(body)
                copy_btn.Text = "Copié ✓"
            else:
                copy_btn.Text = "(vide)"
        except Exception:  # noqa: BLE001 — clipboard peut être lock par une autre app.
            copy_btn.Text = "Échec copie"

    def _on_copy(sender, e):
        _copy_now()

    copy_btn.Click += _on_copy

    def _on_keydown(sender, e):
        # Ctrl+A → select all.
        if e.Control and e.KeyCode == Keys.A:
            textbox.SelectAll()
            e.Handled = True
            e.SuppressKeyPress = True

    textbox.KeyDown += _on_keydown

    if auto_copy:
        _copy_now()

    textbox.Select()
    textbox.DeselectAll()
    form.ShowDialog()
