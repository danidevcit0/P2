# PS202 Game Manager

Aplicación Kivy/Android para trabajar con `gameconsole.db` y `roms/` de una unidad USB mediante Android Storage Access Framework (SAF).

## Qué hace

1. El usuario selecciona la raíz de `Unidad USB`.
2. Comprueba que existen:
   - `gameconsole.db`
   - `roms/`
3. Copia únicamente `gameconsole.db` a almacenamiento privado temporal.
4. Ejecuta la lógica SQLite del gestor.
5. Lee ROMs e imágenes directamente desde la USB mediante SAF.
6. Regenera `gamelist.xml` directamente en la USB.
7. Copia `gameconsole.db` modificado de vuelta a la USB.
8. Borra la copia temporal.

No usa `/storage`, `/mnt/media_rw` ni `/mnt/user`.

## Compilación

Buildozer necesita Linux o macOS. En Windows se puede usar WSL2. La documentación oficial de Kivy recomienda Buildozer para crear el APK.

En Ubuntu/WSL:

    sudo apt update
    sudo apt install -y git zip unzip openjdk-17-jdk python3-pip python3-virtualenv autoconf libtool pkg-config zlib1g-dev libncurses5-dev libncursesw5-dev libtinfo6 cmake libffi-dev libssl-dev automake

Después:

    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install buildozer cython==0.29.34

Entrar en la carpeta del proyecto:

    cd PS202_Game_Manager

Primera compilación:

    buildozer android debug

El APK aparecerá en:

    bin/

También se puede usar:

    buildozer android debug deploy run

para compilar, instalar y ejecutar si ADB detecta el dispositivo.

## Importante

No hace falta conceder "acceso a todos los archivos". La aplicación usa el selector oficial de carpetas de Android y el usuario selecciona la raíz de la unidad USB.

La aplicación debe recibir acceso a la raíz que contiene directamente `gameconsole.db` y `roms/`.
