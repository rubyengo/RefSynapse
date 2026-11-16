"""The F1 shortcuts reference, kept out of the main window."""
from PySide6.QtWidgets import QMessageBox

from .config import NAV_ACTIONS, binding_text, model_navigation_hint


def show_help(win):
    rows = "".join(
        f"<tr><td><b>{label}</b></td><td>"
        + " &nbsp;or&nbsp; ".join(binding_text(b) for b in win.nav["bindings"][k] if b)
        + "</td></tr>" for k, label in NAV_ACTIONS)
    wheel = "zoom" if win.nav["wheel"] == "zoom" else "scroll (Alt + Wheel = zoom)"
    model_controls = model_navigation_hint(win.nav)
    L = win.transform_letters()
    html = f"""
<h3>Navigation - preset: {win.nav.get('preset', 'Custom')}</h3>
<table cellspacing=4>{rows}
<tr><td><b>Wheel</b></td><td>{wheel}</td></tr>
<tr><td><b>Ctrl+Shift + Wheel</b></td><td>window opacity</td></tr>
<tr><td><b>Right click</b></td><td>menu</td></tr>
<tr><td><b>Double click on empty space</b></td><td>fit all</td></tr>
<tr><td><b>Window edge</b></td><td>resize window (frameless mode)</td></tr>
</table>
<h3>Transform selected items</h3>
<table cellspacing=4>
<tr><td><b>{L['move']} / {L['rotate']} / {L['scale']}</b></td><td>move / rotate / scale, then move the mouse</td></tr>
<tr><td><b>LMB, Enter</b></td><td>confirm</td></tr>
<tr><td><b>RMB, Esc</b></td><td>cancel</td></tr>
<tr><td><b>Ctrl (hold)</b></td><td>enable object/grid snapping while moving/scaling; move/rotate steps: 10 px / 15\u00b0</td></tr>
<tr><td><b>X / Y</b></td><td>lock axis while moving</td></tr>
<tr><td><b>Corner / side handle</b></td><td>scale objects; move an individual border on frames; multi-selection gets one shared scale box</td></tr>
<tr><td><b>Alt+S / Alt+R</b></td><td>reset scale / reset rotation</td></tr>
<tr><td><b>[ ] / Shift+H</b></td><td>scale -10% / +10%, flip</td></tr>
<tr><td><b>Ctrl+L</b></td><td>lock / unlock: a locked object cannot be moved, scaled or rotated</td></tr>
</table>
<h3>Export and sharing</h3>
<table cellspacing=4>
<tr><td><b>Ctrl+E</b></td><td>export selected items: image crop, PDF page, video frame, 3D view, group</td></tr>
<tr><td><b>Ctrl+Shift+E</b></td><td>export the whole board as one picture</td></tr>
<tr><td><b>Ctrl+Shift + drag</b></td><td>drag objects out to Explorer, a browser or a messenger as real files</td></tr>
<tr><td><b>Right click → Export</b></td><td>selection as PNG, board as PNG, all document pages, original media files</td></tr>
<tr><td><b>Formats</b></td><td>PNG / JPEG / WebP / BMP; scale 100-300%; dark, white or transparent background</td></tr>
</table>
<h3>Other</h3>
<table cellspacing=4>
<tr><td><b>Ctrl+Z / Ctrl+Y</b></td><td>undo / redo</td></tr>
<tr><td><b>Ctrl+C / Ctrl+X</b></td><td>copy / cut to clipboard; a single image keeps its full-resolution crop</td></tr>
<tr><td><b>Ctrl+V / Ctrl+I</b></td><td>paste / add files; a copied image link is downloaded automatically</td></tr>
<tr><td><b>Drag from a browser</b></td><td>drop a picture or its link straight onto the board</td></tr>
<tr><td><b>Shift+D</b></td><td>duplicate selected objects and immediately move the copies</td></tr>
<tr><td><b>N</b>, double click a note</td><td>annotation with font and color</td></tr>
<tr><td><b>V / hold D</b></td><td>Select / draw while D is held; release D to return to Select</td></tr>
<tr><td><b>T / F</b></td><td>one-shot text / frame; then Select</td></tr>
<tr><td><b>Shift / Ctrl + LMB</b></td><td>add to / remove from selection</td></tr>
<tr><td><b>X / Delete</b></td><td>delete selected objects</td></tr>
<tr><td><b>Title bar</b></td><td>drag to move window; double-click to maximize</td></tr>
<tr><td><b>Ctrl+G / Ctrl+Shift+G / F2</b></td><td>group / ungroup / rename group</td></tr>
<tr><td><b>Double-click / Space / M</b></td><td>video play-pause / play-pause / mute</td></tr>
<tr><td><b>Selected video panel</b></td><td>on the video: seek, volume, Poster, Frame → Image; position and sound are saved</td></tr>
<tr><td><b>Ctrl + move / crop</b></td><td>temporarily enable object/grid snapping; configure it in View or Settings</td></tr>
<tr><td><b>Documents</b></td><td>drop TXT, MD, RTF, HTML, DOCX, ODT, PDF, SVG and more</td></tr>
<tr><td><b>PgUp / PgDown</b></td><td>previous / next page of the selected document or PDF</td></tr>
<tr><td><b>Ctrl + Wheel over PDF</b></td><td>previous / next PDF page</td></tr>
<tr><td><b>3D models</b></td><td>STL, 3MF, OBJ, FBX, glTF/GLB, STEP and PLY</td></tr>
<tr><td><b>3D navigation</b></td><td>{model_controls}; double-click to reset</td></tr>
<tr><td><b>3D shading</b></td><td>Smooth Shading checkbox in the 3D field; saved with the project</td></tr>
<tr><td><b>Alt + Arrows</b></td><td>align</td></tr>
<tr><td><b>Ctrl+Shift+H / V, Ctrl+P</b></td><td>row / column / pack; Arrange → Distribute for equal gaps</td></tr>
<tr><td><b>Home / Z / Ctrl+0</b></td><td>fit all / zoom to selected / 100%</td></tr>
<tr><td><b>C</b></td><td>crop one image; hold Ctrl to snap; Enter or LMB outside applies, Esc cancels</td></tr>
<tr><td><b>Ctrl+F / Ctrl+M</b></td><td>search board / show minimap</td></tr>
<tr><td><b>Ctrl+T / Ctrl+B / F11</b></td><td>on top / frameless / fullscreen</td></tr>
<tr><td><b>Ctrl+,</b></td><td>settings</td></tr>
</table>"""
    QMessageBox.information(win, "RefSynapse - Help", html)