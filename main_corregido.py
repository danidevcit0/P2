# -*- coding: utf-8 -*-
from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView

from jnius import autoclass
from android import activity

import os
import sqlite3
import tempfile
import traceback
import xml.etree.ElementTree as ET


IMAGE_EXTS = [".png", ".jpg", ".jpeg"]

SYSTEM_IDS = {
    "arcade": 2, "cps1": 3, "cps2": 4, "cps3": 5,
    "gb": 6, "gba": 7, "gbc": 8, "gamegear": 9,
    "megadrive": 10, "nes": 11, "ngpc": 12, "pcengine": 13,
    "psp": 14, "psx": 15, "sms": 16, "snes": 17,
    "wonder": 18, "atari2600": 19
}

ROM_EXTS = (
    '.zip', '.7z', '.gba', '.gb', '.gbc', '.nes', '.smc', '.sfc',
    '.bin', '.cue', '.iso', '.img', '.chd', '.cso', '.md', '.gen',
    '.sms', '.gg', '.ws', '.wsc', '.a26', '.a78', '.n64', '.z64', '.v64'
)

Intent = autoclass("android.content.Intent")
Activity = autoclass("android.app.Activity")
DocumentsContract = autoclass("android.provider.DocumentsContract")
Document = autoclass("android.provider.DocumentsContract$Document")
REQUEST_USB_TREE = 5001


class SAFTree:
    DIR_MIME = "vnd.android.document/directory"

    def __init__(self, tree_uri):
        self.tree_uri = tree_uri
        self.resolver = MainActivity.mActivity.getContentResolver()
        self.root_doc_id = DocumentsContract.getTreeDocumentId(tree_uri)
        self.cache = {}

    def clear_cache(self):
        self.cache.clear()

    def root_uri(self):
        return DocumentsContract.buildDocumentUriUsingTree(
            self.tree_uri, self.root_doc_id
        )

    def list_children(self, directory_uri):
        key = str(directory_uri)
        if key in self.cache:
            return self.cache[key]

        children = {}
        children_uri = DocumentsContract.buildChildDocumentsUriUsingTree(
            self.tree_uri,
            DocumentsContract.getDocumentId(directory_uri)
        )

        projection = [
            Document.COLUMN_DOCUMENT_ID,
            Document.COLUMN_DISPLAY_NAME,
            Document.COLUMN_MIME_TYPE
        ]

        cursor = self.resolver.query(
            children_uri, projection, None, None, None
        )
        if cursor is None:
            return children

        try:
            id_index = cursor.getColumnIndex(
                Document.COLUMN_DOCUMENT_ID
            )
            name_index = cursor.getColumnIndex(
                Document.COLUMN_DISPLAY_NAME
            )
            mime_index = cursor.getColumnIndex(
                Document.COLUMN_MIME_TYPE
            )

            while cursor.moveToNext():
                doc_id = cursor.getString(id_index)
                name = cursor.getString(name_index)
                mime = cursor.getString(mime_index)
                child_uri = DocumentsContract.buildDocumentUriUsingTree(
                    self.tree_uri, doc_id
                )
                children[name] = {
                    "uri": child_uri,
                    "name": name,
                    "mime": mime,
                    "is_dir": mime == self.DIR_MIME
                }
        finally:
            cursor.close()

        self.cache[key] = children
        return children

    def resolve(self, relative_path):
        relative_path = relative_path.replace("\\", "/").strip("/")
        if not relative_path:
            return self.root_uri()

        current_uri = self.root_uri()
        for part in relative_path.split("/"):
            children = self.list_children(current_uri)
            item = children.get(part)
            if item is None:
                return None
            current_uri = item["uri"]
        return current_uri

    def read_bytes(self, relative_path):
        uri = self.resolve(relative_path)
        if uri is None:
            raise FileNotFoundError("No existe en USB: " + relative_path)

        inp = self.resolver.openInputStream(uri)
        if inp is None:
            raise IOError("No se pudo abrir: " + relative_path)

        chunks = []
        try:
            buffer = bytearray(64 * 1024)
            while True:
                count = inp.read(buffer)
                if count == -1:
                    break
                if count > 0:
                    chunks.append(bytes(buffer[:count]))
        finally:
            inp.close()
        return b"".join(chunks)

    def write_bytes(self, relative_path, data):
        uri = self.resolve(relative_path)
        if uri is None:
            raise FileNotFoundError("No existe el archivo: " + relative_path)

        out = self.resolver.openOutputStream(uri)
        if out is None:
            raise IOError("No se pudo abrir para escritura: " + relative_path)

        try:
            out.write(data)
            out.flush()
        finally:
            out.close()
        self.clear_cache()

    def create_file(self, parent_relative, filename, mime):
        parent_uri = self.resolve(parent_relative)
        if parent_uri is None:
            raise FileNotFoundError("No existe carpeta: " + parent_relative)

        new_uri = DocumentsContract.createDocument(
            self.resolver, parent_uri, mime, filename
        )
        if new_uri is None:
            raise IOError("No se pudo crear: " + filename)

        self.clear_cache()
        return new_uri

    def write_or_create(self, relative_path, data, mime):
        relative_path = relative_path.replace("\\", "/").strip("/")
        parts = relative_path.split("/")
        filename = parts[-1]
        parent = "/".join(parts[:-1]) if len(parts) > 1 else ""

        existing_uri = self.resolve(relative_path)
        if existing_uri is not None:
            out = self.resolver.openOutputStream(existing_uri)
            if out is None:
                raise IOError("No se pudo abrir: " + relative_path)
            try:
                out.write(data)
                out.flush()
            finally:
                out.close()
            self.clear_cache()
            return

        new_uri = self.create_file(parent, filename, mime)
        out = self.resolver.openOutputStream(new_uri)
        if out is None:
            raise IOError("No se pudo abrir archivo nuevo: " + relative_path)
        try:
            out.write(data)
            out.flush()
        finally:
            out.close()
        self.clear_cache()


def copy_usb_to_local(usb, usb_path, local_path):
    data = usb.read_bytes(usb_path)
    with open(local_path, "wb") as f:
        f.write(data)


def copy_local_to_usb(usb, local_path, usb_path):
    with open(local_path, "rb") as f:
        data = f.read()
    usb.write_bytes(usb_path, data)


def find_image_cached(system, title, image_cache):
    return image_cache.get(system, {}).get(
        (title or "").strip().lower()
    )


def build_image_cache(usb):
    image_cache = {}
    for system in SYSTEM_IDS:
        image_cache[system] = {}
        images_path = "roms/{}/images".format(system)
        images_uri = usb.resolve(images_path)
        if images_uri is None:
            continue

        for name, item in usb.list_children(images_uri).items():
            if item["is_dir"]:
                continue
            base, ext = os.path.splitext(name)
            if ext.lower() in IMAGE_EXTS:
                image_cache[system][base.strip().lower()] = name
    return image_cache


def list_rom_files(usb, system):
    folder_uri = usb.resolve("roms/{}".format(system))
    if folder_uri is None:
        return []

    result = []
    for name, item in usb.list_children(folder_uri).items():
        if item["is_dir"]:
            continue
        if name.lower() == "gamelist.xml":
            continue
        if name.lower().endswith(ROM_EXTS):
            result.append(name)
    return result


def process_database(usb, local_db, log):
    conn = None
    try:
        log("Abriendo SQLite...")
        conn = sqlite3.connect(local_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        log("Creando cache de imágenes...")
        image_cache = build_image_cache(usb)

        templates = {}
        for sysname in SYSTEM_IDS:
            cur.execute(
                "SELECT * FROM game_data_bean "
                "WHERE game_type_name=? LIMIT 1",
                (sysname,)
            )
            row = cur.fetchone()
            if row:
                templates[sysname] = dict(row)

        log("Plantillas encontradas: {}".format(len(templates)))

        cur.execute("SELECT * FROM game_data_bean")
        rows = cur.fetchall()
        total_rows = len(rows)

        for i, r in enumerate(rows, 1):
            if i % 500 == 0:
                log("[AUDITORIA] {}/{}".format(i, total_rows))

            row = dict(r)
            path = row.get("game_details_path") or ""
            parts = path.replace("\\", "/").split("/")
            if len(parts) < 3:
                continue

            system = parts[-2]
            display_name = row.get("en_US") or row.get("game_name") or ""

            img = find_image_cached(system, display_name, image_cache)
            if not img:
                img = find_image_cached(
                    system, row.get("game_name") or "", image_cache
                )

            if img:
                row_image = "./images/" + img
                row_icon = (
                    "/storage/sdcard1/roms/{}/images/{}"
                    .format(system, img)
                )
            else:
                row_image = ""
                row_icon = ""

            cur.execute(
                """
                UPDATE game_data_bean
                SET image=?,
                    game_details_icon_path=?
                WHERE game_id=?
                """,
                (row_image, row_icon, row["game_id"])
            )

        cur.execute("SELECT MAX(game_id) FROM game_data_bean")
        next_id = (cur.fetchone()[0] or 0) + 1

        cur.execute("SELECT game_details_path FROM game_data_bean")
        existing = set(x[0] for x in cur.fetchall() if x[0])

        inserted = 0

        for system in SYSTEM_IDS:
            if usb.resolve("roms/{}".format(system)) is None:
                continue
            if system not in templates:
                continue

            roms = list_rom_files(usb, system)
            log("[ROM] {}: {} archivos".format(system, len(roms)))

            for fname in roms:
                rel = "roms/{}/{}".format(system, fname)
                if rel in existing:
                    continue

                title = os.path.splitext(fname)[0]
                row = templates[system].copy()

                row["game_id"] = next_id
                row["game_name"] = title
                row["en_US"] = title
                row["zh_CN"] = title
                row["zh_TW"] = title
                row["ko_KR"] = title
                row["game_name_pin_yin"] = title.lower()
                row["game_name_tag"] = ""
                row["game_net_path"] = ""
                row["game_icon_net_path"] = ""
                row["base_tf_card_path"] = ""
                row["game_details_path"] = rel
                row["xml_path"] = "./" + fname

                img = find_image_cached(system, title, image_cache)
                if img:
                    row["image"] = "./images/" + img
                    row["game_details_icon_path"] = (
                        "/storage/sdcard1/roms/{}/images/{}"
                        .format(system, img)
                    )
                else:
                    row["image"] = ""
                    row["game_details_icon_path"] = ""

                cols = list(row.keys())
                vals = [row[c] for c in cols]
                sql = (
                    "INSERT INTO game_data_bean ({}) VALUES ({})"
                    .format(",".join(cols), ",".join(["?"] * len(cols)))
                )
                cur.execute(sql, vals)

                existing.add(rel)
                next_id += 1
                inserted += 1

        log("ROM nuevas insertadas: {}".format(inserted))
        conn.commit()

        cur.execute(
            """
            SELECT *
            FROM game_data_bean
            ORDER BY game_type_name,
                     COALESCE(en_US, game_name)
            """
        )

        systems = {}
        for row in cur.fetchall():
            d = dict(row)
            systems.setdefault(d["game_type_name"], []).append(d)

        for system in systems:
            systems[system].sort(
                key=lambda g: (
                    (g.get("en_US") or g.get("game_name") or "").lower()
                )
            )

        total_systems = len(systems)

        for current, (system, games) in enumerate(systems.items(), 1):
            log("[XML] {}/{} {}".format(
                current, total_systems, system
            ))

            if usb.resolve("roms/{}".format(system)) is None:
                continue

            root = ET.Element("gameList")

            for game in games:
                node = ET.SubElement(root, "game")
                rom_file = os.path.basename(game["game_details_path"])

                ET.SubElement(node, "gameid").text = str(game["game_id"])
                ET.SubElement(node, "path").text = "./" + rom_file
                ET.SubElement(node, "image").text = str(
                    game.get("image") or ""
                )
                ET.SubElement(node, "video_id").text = str(
                    game.get("video_id") or 0
                )
                ET.SubElement(node, "class_type").text = str(
                    game.get("game_type_id") or 0
                )
                ET.SubElement(node, "game_type").text = "0"
                ET.SubElement(node, "timer").text = str(
                    game.get("timer") or system
                )
                ET.SubElement(node, "zh_CN").text = str(
                    game.get("zh_CN") or game["game_name"]
                )
                ET.SubElement(node, "en_US").text = str(
                    game.get("en_US") or game["game_name"]
                )
                ET.SubElement(node, "zh_TW").text = str(
                    game.get("zh_TW") or game["game_name"]
                )
                ET.SubElement(node, "ko_KR").text = str(
                    game.get("ko_KR") or game["game_name"]
                )
                ET.SubElement(node, "name").text = str(
                    game["game_name"]
                )

            xml_data = ET.tostring(
                root, encoding="utf-8", xml_declaration=True
            )

            usb.write_or_create(
                "roms/{}/gamelist.xml".format(system),
                xml_data,
                "application/xml"
            )

        conn.commit()
        log("Proceso SQLite/XML terminado.")

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


class PS202App(App):
    def build(self):
        global MainActivity
        MainActivity = autoclass("org.kivy.android.PythonActivity")

        root = BoxLayout(
            orientation="vertical",
            padding=18,
            spacing=12
        )

        self.status = Label(
            text="PS202 Game Manager\nSelecciona la raíz de Unidad USB.",
            font_size="18sp"
        )
        root.add_widget(self.status)

        self.button = Button(
            text="SELECCIONAR UNIDAD USB",
            size_hint_y=None,
            height=60
        )
        self.button.bind(on_release=self.select_usb)
        root.add_widget(self.button)

        scroll = ScrollView()
        self.log_label = Label(
            text="",
            size_hint_y=None,
            halign="left",
            valign="top",
            font_size="14sp"
        )
        self.log_label.bind(
            texture_size=lambda obj, size: setattr(obj, "height", size[1])
        )
        scroll.add_widget(self.log_label)
        root.add_widget(scroll)

        self.usb = None
        self.local_db = None
        return root

    def log(self, text):
        print(text)
        Clock.schedule_once(
            lambda dt: self._append_log(str(text)), 0
        )

    def _append_log(self, text):
        self.log_label.text += text + "\n"

    def select_usb(self, *_):
        self.button.disabled = True
        self.log("Abriendo selector Android...")

        try:
            activity.bind(
                on_activity_result=self.on_activity_result
            )

            intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
            intent.addFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION
                | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION
            )

            MainActivity.mActivity.startActivityForResult(
                intent, REQUEST_USB_TREE
            )

        except Exception:
            try:
                activity.unbind(
                    on_activity_result=self.on_activity_result
                )
            except Exception:
                pass
            self.button.disabled = False
            self.log("ERROR abriendo selector:")
            self.log(traceback.format_exc())

    def on_activity_result(self, request_code, result_code, data):
        if request_code != REQUEST_USB_TREE:
            return

        try:
            activity.unbind(
                on_activity_result=self.on_activity_result
            )
        except Exception:
            pass

        if result_code != Activity.RESULT_OK or data is None:
            self.button.disabled = False
            self.log("Selección cancelada.")
            return

        try:
            uri = data.getData()
            if uri is None:
                raise RuntimeError("Android no devolvió ninguna URI.")

            flags = data.getFlags() & (
                Intent.FLAG_GRANT_READ_URI_PERMISSION
                | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            )

            try:
                MainActivity.mActivity.getContentResolver().takePersistableUriPermission(
                    uri, flags
                )
            except Exception as e:
                self.log("Aviso: permiso persistente no guardado: {}".format(e))

            self.usb = SAFTree(uri)

            if self.usb.resolve("gameconsole.db") is None:
                raise FileNotFoundError(
                    "No encuentro gameconsole.db en la raíz seleccionada."
                )

            if self.usb.resolve("roms") is None:
                raise FileNotFoundError(
                    "No encuentro la carpeta roms/ en la raíz seleccionada."
                )

            self.status.text = "USB seleccionado. Procesando..."
            self.log("USB seleccionado correctamente.")
            self.log("Encontrados gameconsole.db y roms/.")

            Clock.schedule_once(
                lambda dt: self.run_process(), 0.2
            )

        except Exception:
            self.button.disabled = False
            self.log("ERROR procesando selección:")
            self.log(traceback.format_exc())

    def run_process(self):
        try:
            fd, self.local_db = tempfile.mkstemp(
                prefix="ps202_", suffix=".db"
            )
            os.close(fd)

            self.log("Copiando SOLO gameconsole.db al almacenamiento privado...")
            copy_usb_to_local(
                self.usb, "gameconsole.db", self.local_db
            )

            self.log("Procesando SQLite...")
            process_database(
                self.usb, self.local_db, self.log
            )

            self.log("Escribiendo gameconsole.db modificado al USB...")
            copy_local_to_usb(
                self.usb, self.local_db, "gameconsole.db"
            )

            self.status.text = "FINALIZADO"
            self.log("")
            self.log("================================")
            self.log("FINALIZADO CORRECTAMENTE")
            self.log("================================")

        except Exception:
            self.status.text = "ERROR"
            self.log("ERROR DURANTE EL PROCESO:")
            self.log(traceback.format_exc())

        finally:
            if self.local_db:
                try:
                    if os.path.exists(self.local_db):
                        os.remove(self.local_db)
                except Exception:
                    pass
            self.button.disabled = False


if __name__ == "__main__":
    PS202App().run()
