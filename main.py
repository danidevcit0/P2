# -*- coding: utf-8 -*-
from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window

from jnius import autoclass
from android import activity

import os
import sqlite3
import tempfile
import traceback
import threading
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
PythonActivity = autoclass("org.kivy.android.PythonActivity")
DocumentsContract = autoclass("android.provider.DocumentsContract")
Document = autoclass("android.provider.DocumentsContract$Document")
Uri = autoclass("android.net.Uri")
REQUEST_USB_TREE = 5001


class SAFTree:
    DIR_MIME = "vnd.android.document/directory"

    @staticmethod
    def _java_string(value):
        """Convierte de forma segura un String Java/PyJNIus a str Python."""
        if value is None:
            return None
        try:
            return value.toString()
        except Exception:
            return str(value)

    @classmethod
    def _normalize_uri(cls, value, label="URI"):
        """
        Fuerza una URI nueva de Android a partir de su texto.

        Esto evita pasar directamente a ContentResolver un wrapper PyJNIus
        que pueda haber quedado como una referencia Java no valida.
        """
        if value is None:
            raise RuntimeError("SAF: {} es NULL.".format(label))

        try:
            text = cls._java_string(value)
        except Exception as e:
            raise RuntimeError(
                "SAF: no se pudo convertir {} a texto: {}".format(label, e)
            )

        if text is None:
            raise RuntimeError("SAF: {} produjo texto NULL.".format(label))

        text = text.strip()
        if not text:
            raise RuntimeError("SAF: {} produjo texto vacío.".format(label))

        try:
            parsed = Uri.parse(text)
        except Exception as e:
            raise RuntimeError(
                "SAF: Uri.parse() falló para {}: {}\nTexto={}".format(
                    label, e, text
                )
            )

        if parsed is None:
            raise RuntimeError(
                "SAF: Uri.parse() devolvió NULL para {}.\nTexto={}".format(
                    label, text
                )
            )

        try:
            scheme = cls._java_string(parsed.getScheme())
            authority = cls._java_string(parsed.getAuthority())
        except Exception as e:
            raise RuntimeError(
                "SAF: no se pudo inspeccionar {}: {}\nTexto={}".format(
                    label, e, text
                )
            )

        if not scheme or not authority:
            raise RuntimeError(
                "SAF: {} no parece una URI content válida.\n"
                "scheme={} authority={} texto={}".format(
                    label, scheme, authority, text
                )
            )

        return parsed

    @classmethod
    def _uri_debug(cls, uri):
        if uri is None:
            return "NULL"
        try:
            text = cls._java_string(uri)
        except Exception:
            text = "<no se pudo obtener toString()>"
        try:
            scheme = cls._java_string(uri.getScheme())
        except Exception:
            scheme = "?"
        try:
            authority = cls._java_string(uri.getAuthority())
        except Exception:
            authority = "?"
        try:
            path = cls._java_string(uri.getPath())
        except Exception:
            path = "?"
        return "text={} | scheme={} | authority={} | path={}".format(
            text, scheme, authority, path
        )

    def __init__(self, tree_uri):
        # IMPORTANTE: NO guardamos objetos Java Uri/ContentResolver para
        # reutilizarlos desde otro hilo. PyJNIus puede representarlos como
        # LocalRef; al pasar de on_activity_result (UI) al worker ese ref
        # puede quedar inválido (el traceback mostraba self=<LocalRef obj=0x0>).
        # Guardamos únicamente texto Python y creamos los objetos Java en el
        # mismo hilo que realiza cada operación SAF.
        tree_text = self._java_string(tree_uri)
        if tree_text is None or not tree_text.strip():
            raise RuntimeError("SAF: treeUri seleccionada es NULL/vacía.")
        tree_text = tree_text.strip()

        # Parseo inicial solo para validar y obtener el documentId. El objeto
        # Java NO se conserva como atributo de la clase.
        tree_obj = self._normalize_uri(tree_text, "treeUri seleccionada")
        self.tree_uri_text = tree_text

        try:
            root_id = DocumentsContract.getTreeDocumentId(tree_obj)
        except Exception as e:
            raise RuntimeError(
                "SAF: no se pudo obtener getTreeDocumentId().\n"
                "treeUri: {}\n{}".format(
                    self._uri_debug(tree_obj), e
                )
            )

        self.root_doc_id = self._java_string(root_id)
        if self.root_doc_id is None or not self.root_doc_id.strip():
            raise RuntimeError(
                "SAF: getTreeDocumentId() devolvió NULL/vacío.\n"
                "treeUri: {}".format(self._uri_debug(tree_obj))
            )
        self.root_doc_id = self.root_doc_id.strip()
        self.cache = {}

    def _fresh_tree_uri(self):
        # Cada llamada obtiene un objeto Uri nuevo en el hilo actual.
        return self._normalize_uri(self.tree_uri_text, "treeUri actual")

    def _fresh_resolver(self):
        # No conservamos ContentResolver obtenido en otro hilo.
        activity_obj = PythonActivity.mActivity
        if activity_obj is None:
            raise RuntimeError("SAF: PythonActivity.mActivity es NULL.")
        resolver = activity_obj.getContentResolver()
        if resolver is None:
            raise RuntimeError("SAF: ContentResolver es NULL.")
        return resolver

    def clear_cache(self):
        self.cache.clear()

    def root_uri(self):
        tree_uri = self._fresh_tree_uri()
        try:
            raw_uri = DocumentsContract.buildDocumentUriUsingTree(
                tree_uri, self.root_doc_id
            )
        except Exception as e:
            raise RuntimeError(
                "SAF: buildDocumentUriUsingTree() falló para la raíz.\n"
                "documentId={}\nTREE={}\n{}".format(
                    self.root_doc_id, self.tree_uri_text, e
                )
            )

        return self._normalize_uri(raw_uri, "URI raíz")

    def list_children(self, parent_doc_id=None, debug_path="/"):
        """Enumera UNA carpeta SAF y devuelve solo metadatos Python.

        No crea una Uri Java por cada hijo. Esto es deliberado: una carpeta
        con miles de ROMs no debe generar miles de wrappers JNI que luego no
        necesitamos. La Uri de un hijo se construye solo cuando ese hijo se
        necesita realmente (por ejemplo, una carpeta o gamelist.xml).
        """
        if parent_doc_id is None:
            raise RuntimeError(
                "SAF: documentId nulo al listar: {}".format(debug_path)
            )

        parent_doc_id = self._java_string(parent_doc_id)
        if parent_doc_id is None or not parent_doc_id.strip():
            raise RuntimeError(
                "SAF: documentId vacío para la carpeta: {}".format(debug_path)
            )
        parent_doc_id = parent_doc_id.strip()

        cache_key = "DOCID:" + parent_doc_id
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        tree_uri = self._fresh_tree_uri()
        resolver = self._fresh_resolver()

        try:
            children_uri = DocumentsContract.buildChildDocumentsUriUsingTree(
                tree_uri, parent_doc_id
            )
        except Exception as e:
            raise RuntimeError(
                "SAF: no se pudo construir URI de hijos para: {}\n"
                "documentId={}\nTREE={}\n{}".format(
                    debug_path, parent_doc_id, self.tree_uri_text, e
                )
            )

        children_uri = self._normalize_uri(
            children_uri, "URI hijos {}".format(debug_path)
        )

        projection = [
            self._java_string(Document.COLUMN_DOCUMENT_ID),
            self._java_string(Document.COLUMN_DISPLAY_NAME),
            self._java_string(Document.COLUMN_MIME_TYPE),
        ]

        try:
            cursor = resolver.query(children_uri, projection, None, None, None)
        except Exception as e:
            raise RuntimeError(
                "SAF: ContentResolver.query() falló en: {}\n"
                "documentId={}\nchildrenUri={}\nTREE={}\n{}".format(
                    debug_path, parent_doc_id,
                    self._uri_debug(children_uri), self.tree_uri_text, e
                )
            )

        if cursor is None:
            raise RuntimeError(
                "SAF: ContentResolver.query() devolvió NULL en: {}\n"
                "childrenUri={}\nTREE={}".format(
                    debug_path, self._uri_debug(children_uri), self.tree_uri_text
                )
            )

        children = {}
        try:
            id_index = cursor.getColumnIndex(projection[0])
            name_index = cursor.getColumnIndex(projection[1])
            mime_index = cursor.getColumnIndex(projection[2])

            if id_index < 0 or name_index < 0 or mime_index < 0:
                raise RuntimeError(
                    "SAF: el proveedor USB no devolvió las columnas esperadas en: {}"
                    .format(debug_path)
                )

            while cursor.moveToNext():
                doc_id = self._java_string(cursor.getString(id_index))
                name = self._java_string(cursor.getString(name_index))
                mime = self._java_string(cursor.getString(mime_index))

                if doc_id is None or not doc_id.strip():
                    raise RuntimeError(
                        "SAF: documento hijo sin documentId en: {}".format(debug_path)
                    )
                if name is None:
                    raise RuntimeError(
                        "SAF: documento hijo sin displayName en: {}\n"
                        "documentId={}".format(debug_path, doc_id)
                    )

                doc_id = doc_id.strip()
                children[name] = {
                    "doc_id": doc_id,
                    "name": name,
                    "mime": mime,
                    "is_dir": mime == self.DIR_MIME,
                }
        finally:
            cursor.close()

        self.cache[cache_key] = children
        return children

    def _child_uri(self, doc_id, label="URI hijo"):
        doc_id = self._java_string(doc_id)
        if doc_id is None or not doc_id.strip():
            raise RuntimeError("SAF: documentId vacío para {}.".format(label))
        raw = DocumentsContract.buildDocumentUriUsingTree(
            self._fresh_tree_uri(), doc_id.strip()
        )
        return self._normalize_uri(raw, label)

    def resolve_entry(self, relative_path):
        """Resuelve una ruta SAF. Solo crea Uris Java para los componentes necesarios."""
        relative_path = relative_path.replace("\\", "/").strip("/")
        if not relative_path:
            return {
                "uri": self.root_uri(),
                "doc_id": self.root_doc_id,
                "name": "",
                "mime": self.DIR_MIME,
                "is_dir": True,
            }

        current_doc_id = self.root_doc_id
        item = None
        current_path = []

        for part in relative_path.split("/"):
            current_path.append(part)
            debug_path = "/" + "/".join(current_path)
            children = self.list_children(
                parent_doc_id=current_doc_id,
                debug_path="/" + "/".join(current_path[:-1]) or "/"
            )
            item = children.get(part)
            if item is None:
                return None
            current_doc_id = item["doc_id"]

        item = dict(item)
        item["uri"] = self._child_uri(
            item["doc_id"], "URI {}".format(relative_path)
        )
        return item

    def resolve(self, relative_path):
        entry = self.resolve_entry(relative_path)
        return None if entry is None else entry.get("uri")

    def read_bytes(self, relative_path):
        uri = self.resolve(relative_path)
        if uri is None:
            raise FileNotFoundError("No existe en USB: " + relative_path)

        uri = self._normalize_uri(uri, "URI lectura {}".format(relative_path))
        inp = self._fresh_resolver().openInputStream(uri)
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

        uri = self._normalize_uri(uri, "URI escritura {}".format(relative_path))
        out = self._fresh_resolver().openOutputStream(uri)
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

        parent_uri = self._normalize_uri(
            parent_uri, "URI carpeta creación {}".format(parent_relative)
        )
        new_uri = DocumentsContract.createDocument(
            self._fresh_resolver(), parent_uri, mime, filename
        )
        if new_uri is None:
            raise IOError("No se pudo crear: " + filename)

        return self._normalize_uri(new_uri, "URI archivo creado {}".format(filename))

    def write_or_create_in_folder(self, folder_entry, filename, data, mime,
                                  existing_entries=None):
        """Escribe/crea un archivo usando una carpeta ya enumerada.
        No vuelve a listar ni resolver la carpeta.
        """
        if folder_entry is None:
            raise FileNotFoundError("Carpeta SAF nula")

        parent_uri = self._child_uri(
            folder_entry["doc_id"], "URI carpeta escritura"
        )

        existing_uri = None
        if existing_entries is not None:
            item = existing_entries.get(filename)
            if item is not None:
                existing_uri = self._child_uri(
                    item["doc_id"], "URI archivo {}".format(filename)
                )

        if existing_uri is None:
            target_uri = DocumentsContract.createDocument(
                self._fresh_resolver(), parent_uri, mime, filename
            )
            if target_uri is None:
                raise IOError("No se pudo crear: " + filename)
            target_uri = self._normalize_uri(
                target_uri, "URI archivo creado {}".format(filename)
            )
        else:
            target_uri = existing_uri

        out = self._fresh_resolver().openOutputStream(target_uri)
        if out is None:
            raise IOError("No se pudo abrir para escritura: " + filename)
        try:
            out.write(data)
            out.flush()
        finally:
            out.close()
        self.clear_cache()

    def write_or_create(self, relative_path, data, mime):
        relative_path = relative_path.replace("\\", "/").strip("/")
        parts = relative_path.split("/")
        filename = parts[-1]
        parent = "/".join(parts[:-1]) if len(parts) > 1 else ""

        existing_uri = self.resolve(relative_path)
        if existing_uri is not None:
            existing_uri = self._normalize_uri(
                existing_uri, "URI escritura {}".format(relative_path)
            )
            out = self._fresh_resolver().openOutputStream(existing_uri)
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
        out = self._fresh_resolver().openOutputStream(new_uri)
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


def build_image_cache(usb, rom_folder_entries):
    image_cache = {}
    for system in SYSTEM_IDS:
        image_cache[system] = {}
        rom_folder = rom_folder_entries.get(system)
        if rom_folder is None:
            continue

        # La carpeta images es un hijo de roms/<system>. La enumeración de
        # roms/<system> ya se hizo una sola vez, por lo que aquí solo
        # resolvemos y leemos la carpeta images cuando realmente existe.
        system_entries = usb.list_children(
            parent_doc_id=rom_folder["doc_id"],
            debug_path="roms/{}/".format(system)
        )
        images_item = system_entries.get("images")
        if images_item is None or not images_item["is_dir"]:
            continue

        image_entries = usb.list_children(
            parent_doc_id=images_item["doc_id"],
            debug_path="roms/{}/images".format(system)
        )
        for name, item in image_entries.items():
            if item["is_dir"]:
                continue
            base, ext = os.path.splitext(name)
            if ext.lower() in IMAGE_EXTS:
                image_cache[system][base.strip().lower()] = name

    return image_cache


def list_rom_files_from_entries(entries):
    """Filtra ROMs a partir de UNA enumeración SAF ya realizada."""
    result = []
    for name, item in entries.items():
        if item["is_dir"]:
            continue
        lower = name.lower()
        if lower == "gamelist.xml":
            continue
        if lower.endswith(ROM_EXTS):
            result.append(name)
    return result


def process_database(usb, local_db, log):
    conn = None
    try:
        log("Abriendo SQLite...")
        conn = sqlite3.connect(local_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # IMPORTANTE: SAF es costoso. Enumeramos cada carpeta ROM UNA sola vez
        # y conservamos el resultado en memoria Python para usarlo tanto en
        # SQLite como en la generación de gamelist.xml.
        log("Leyendo carpetas ROM UNA sola vez para los gamelist...")
        rom_entries = {}
        rom_files = {}
        rom_folder_entries = {}
        for system in SYSTEM_IDS:
            folder_entry = usb.resolve_entry("roms/{}".format(system))
            if folder_entry is None:
                continue
            folder_path = "roms/{}".format(system)
            entries = usb.list_children(
                parent_doc_id=folder_entry["doc_id"],
                debug_path=folder_path
            )
            rom_entries[system] = entries
            rom_folder_entries[system] = folder_entry
            rom_files[system] = list_rom_files_from_entries(entries)
            log("[ROM] {}: {} ROMs (1 lectura SAF)".format(
                system, len(rom_files[system])
            ))

        log("Creando cache de imágenes...")
        image_cache = build_image_cache(usb, rom_folder_entries)

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
            folder_entry = rom_folder_entries.get(system)
            if folder_entry is None:
                continue
            if system not in templates:
                continue

            roms = rom_files.get(system, [])

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

            folder_entry = rom_folder_entries.get(system)
            if folder_entry is None:
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

            # La carpeta y sus hijos ya fueron leídos al principio.
            # Reutilizamos esa información: NO hacemos otra consulta SAF.
            usb.write_or_create_in_folder(
                folder_entry,
                "gamelist.xml",
                xml_data,
                "application/xml",
                existing_entries=rom_entries.get(system)
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
        
        root = BoxLayout(
            orientation="vertical",
            padding=(18, 14),
            spacing=10
        )

        self.status = Label(
            text="PS202 Game Manager\nSelecciona la raíz de Unidad USB.\nNo se copiarán ROM ni imágenes.",
            font_size="18sp",
            size_hint_y=None,
            height=86,
            halign="center",
            valign="middle",
            text_size=(Window.width - 36, 86),
        )
        root.add_widget(self.status)

        self.button = Button(
            text="SELECCIONAR UNIDAD USB",
            size_hint_y=None,
            height=58,
            font_size="16sp"
        )
        self.button.bind(on_release=self.select_usb)
        root.add_widget(self.button)

        self.log_scroll = ScrollView(
            do_scroll_x=False,
            do_scroll_y=True,
            bar_width="6dp"
        )
        self.log_label = Label(
            text="",
            size_hint_y=None,
            size_hint_x=1,
            halign="left",
            valign="top",
            font_size="13sp",
            padding=(6, 8),
            text_size=(Window.width - 48, None)
        )
        self.log_label.bind(
            width=lambda obj, value: setattr(
                obj, "text_size", (max(1, value - 12), None)
            ),
            texture_size=lambda obj, size: setattr(
                obj, "height", max(size[1], 20)
            )
        )
        self.log_scroll.add_widget(self.log_label)
        root.add_widget(self.log_scroll)

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
        Clock.schedule_once(
            lambda dt: setattr(self.log_scroll, "scroll_y", 0), 0
        )

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

            self.log("Lanzando ACTION_OPEN_DOCUMENT_TREE...")
            # android.activity no expone startActivityForResult en esta
            # versión de python-for-android. La Activity real de Kivy sí.
            current_activity = PythonActivity.mActivity
            if current_activity is None:
                raise RuntimeError("PythonActivity.mActivity es NULL.")
            current_activity.startActivityForResult(intent, REQUEST_USB_TREE)
            self.log("Selector Android lanzado.")

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

        if result_code != -1 or data is None:
            self.button.disabled = False
            self.log("Selección cancelada.")
            return

        try:
            uri = data.getData()
            if uri is None:
                raise RuntimeError("Android no devolvió ninguna URI.")

            # Re-parseamos la URI recibida del Intent para obtener un objeto
            # android.net.Uri nuevo y estable antes de usar JNI/SAF.
            uri_text = SAFTree._java_string(uri)
            if uri_text is None or not uri_text.strip():
                raise RuntimeError("Android devolvió una URI vacía.")
            uri = SAFTree._normalize_uri(uri_text, "URI seleccionada")
            self.log("TREE URI: {}".format(SAFTree._uri_debug(uri)))

            flags = data.getFlags() & (
                Intent.FLAG_GRANT_READ_URI_PERMISSION
                | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            )

            try:
                PythonActivity.mActivity.getContentResolver().takePersistableUriPermission(
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
                lambda dt: self.start_process_worker(), 0.2
            )

        except Exception:
            self.button.disabled = False
            self.log("ERROR procesando selección:")
            self.log(traceback.format_exc())

    def start_process_worker(self):
        self.button.disabled = True
        self.status.text = "Procesando USB...\nNo cierres la aplicación."
        threading.Thread(
            target=self.run_process,
            daemon=True
        ).start()

    def _finish_success(self):
        self.status.text = "FINALIZADO"
        self.log("")
        self.log("================================")
        self.log("FINALIZADO CORRECTAMENTE")
        self.log("================================")

    def _finish_error(self, error_text):
        self.status.text = "ERROR"
        self.log("ERROR DURANTE EL PROCESO:")
        self.log(error_text)

    def run_process(self):
        error_text = None
        try:
            fd, self.local_db = tempfile.mkstemp(
                prefix="ps202_", suffix=".db"
            )
            os.close(fd)

            self.log("PASO 1/5 — Copiando SOLO gameconsole.db al almacenamiento temporal...")
            copy_usb_to_local(
                self.usb, "gameconsole.db", self.local_db
            )

            self.log("PASO 2/5 — Procesando SQLite...")
            process_database(
                self.usb, self.local_db, self.log
            )

            self.log("PASO 4/5 — Escribiendo SOLO gameconsole.db modificado en la USB...")
            copy_local_to_usb(
                self.usb, self.local_db, "gameconsole.db"
            )

            Clock.schedule_once(lambda dt: self._finish_success(), 0)

        except Exception:
            error_text = traceback.format_exc()
            Clock.schedule_once(
                lambda dt, e=error_text: self._finish_error(e), 0
            )

        finally:
            if self.local_db:
                try:
                    if os.path.exists(self.local_db):
                        os.remove(self.local_db)
                except Exception:
                    pass
            Clock.schedule_once(
                lambda dt: setattr(self.button, "disabled", False), 0
            )


if __name__ == "__main__":
    PS202App().run()
