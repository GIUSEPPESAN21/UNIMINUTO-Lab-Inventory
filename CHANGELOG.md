# Changelog

## v1.5.0 — Registro abierto a cualquier correo .edu/.edu.co + ID Estudiante

- **Dominio institucional generalizado**: el registro ya no esta restringido
  al dominio `uniminuto.edu.co` (ni a ningun dominio configurado por
  Secrets); ahora acepta cualquier correo cuyo dominio termine en `.edu` o
  `.edu.co`, para que otros laboratorios puedan usar la misma app con su
  propio correo institucional. Se elimino la variable `ALLOWED_EMAIL_DOMAINS`.
- **Nuevo campo obligatorio "ID Estudiante"** en el registro
  (`core/storage.py`: columna `student_id` en la hoja `users`), visible
  despues en Mi perfil y en Usuarios (solo maestro).
- El registro ahora exige, sin excepcion, **todos** sus campos: nombre
  completo (nombre y apellido, no una sola palabra), ID Estudiante, correo
  institucional valido, programa academico o departamento, y contraseña de
  al menos 8 caracteres.

## v1.4.0 — Nomenclatura GLIOPS V3 y etiquetas imprimibles

- **Nueva nomenclatura de codigos de inventario** (Canvas de Estructura y
  Codificacion GLIOPS V3), validada con expresiones regulares en
  `core/barcode.py` y aplicada al dar de alta items nuevos (no revalida
  items ya existentes, para no romper catalogos cargados previamente):
  - **Estandar de 5 niveles, 100% numerico**:
    `[ESTANTERIA]-[PISO]-[CONTENEDOR]-[CAJA]-[ITEM]` (ej. `1-2-05-12-001`).
    Estanteria 1 a 3, Piso 1 a 6; Contenedor/Caja/Item son consecutivos
    positivos (con o sin ceros a la izquierda).
  - **Mesas de trabajo**: `M1-E[n]` o `M2-E[n]` (ej. `M1-E2`).
  - **Exhibicion Lego**: `E3-LM[n]` (ej. `E3-LM07`).
  - `core.barcode.scan()` ahora interpreta el codigo escaneado y expone sus
    componentes (`parsed`); Escanear muestra ese desglose ("Estantería 1 ·
    Piso 2 · Contenedor 05 · Caja 12 · Ítem 001").
- **Generacion de etiquetas imprimibles** (`core/labels.py`): codigo de
  barras Code128 centrado + el codigo en texto legible por humanos abajo,
  compuestos con Pillow sobre un lienzo de 384x192px (aprox. 50x25mm a
  203dpi), listo para imprimirse en una termica SAT TT 460. Botón
  "🏷️ Descargar etiqueta" en el catalogo de Inventario y en Escanear.
- Plantilla CSV de importacion masiva actualizada a la nueva nomenclatura.
- **Bug corregido**: `core.storage.firestore_retry` reintentaba errores de
  validacion (`ValueError`, no transitorios) y su `raise` final, fuera de
  cualquier bloque `except`, enmascaraba el error real con
  `RuntimeError: No active exception to reraise`. Ahora los errores de
  validacion se propagan de inmediato y las fallas transitorias reintentadas
  relanzan la excepcion original.

## v1.3.0 — Modo oscuro real y legibilidad en celular

- **Bug de modo oscuro corregido**: la paleta oscura solo se activaba con el
  atributo `[data-theme="dark"]`, que no siempre está presente en todos los
  navegadores/dispositivos (especialmente en celulares con "Usar
  configuración del sistema"). Ahora también reacciona a la preferencia real
  del sistema operativo (`prefers-color-scheme: dark`), evitando que quede
  texto oscuro sobre fondo oscuro (o viceversa) y los nombres "desaparezcan".
- **Legibilidad en celular**: los `st.metric` (usados en Inicio, Escanear,
  Préstamos, Reportes, etc.) truncaban con "..." las etiquetas y valores
  largos en pantallas angostas; ahora hacen salto de línea. Los títulos de
  página se reducen de tamaño en pantallas pequeñas (`max-width: 640px`) y
  se recorta el padding lateral para aprovechar mejor el espacio.
- El nombre del usuario en el chip del sidebar ahora puede partirse en
  varias líneas en vez de desbordarse si es muy largo.

## v1.2.0 — Interfaz centrada y nueva nomenclatura de contenedores

- **Logo y títulos realmente centrados** en toda la app: nuevo helper
  `core/ui.centered_logo` (HTML puro, no depende de columnas) y
  `core/ui.page_header` para un encabezado consistente en cada página
  (login, sidebar, Inicio, Escanear, Inventario, Préstamos, Usuarios,
  Reportes, Mi perfil, Acerca de).
- **Nueva nomenclatura de contenedores** (centralizada en `core/labels.py`,
  la única fuente de estos nombres para toda la app):
  - `master` → **Contenedor Principal** (la caja, kit o gabinete físico).
  - `child` → **Contenedor de Característica** (una subdivisión dentro del
    Contenedor Principal para una característica concreta, ej. "Resistencias
    220 Ω", "Tornillos M4").
  - `standalone` → **Ítem Individual**.
  - Los formularios de alta ahora piden explícitamente el nombre como
    "Nombre / Característica" cuando se crea un Contenedor de Característica.
- Mensajes de error y validaciones de jerarquía (`core/storage.py`,
  `core/loans.py`) actualizados a la nueva nomenclatura.
- "Acerca de" rediseñada con un layout completamente centrado y apilado en
  vez de la columna logo+texto asimétrica anterior.

## v1.1.0 — Rebranding UNIMINUTO y mejoras de experiencia

- El proyecto pasa a llamarse **Inventario de Laboratorio UNIMINUTO**, con el
  logo oficial de la institución (Wikimedia Commons, CC BY-SA 4.0) en el
  login, la barra lateral y la página "Acerca de".
- Dominio institucional por defecto restringido a `uniminuto.edu.co`.
- Navegación agrupada por secciones (Principal, Gestión del laboratorio,
  Administración, Mi cuenta) en vez de una lista plana de páginas.
- Guía rápida de uso en la pantalla de Inicio y mensajes de estado vacío
  cuando el inventario todavía no tiene productos cargados.
- Base de datos del laboratorio entregada **vacía** desde el primer
  despliegue.

## v1.0.0 — Producto inicial

Primera versión estable: inventario jerárquico (maestro/hijo), login por
roles (estudiante/profesor/maestro), préstamos (salida/reingreso) y
persistencia en Excel sincronizado a GitHub.

### Añadido
- Suite de pruebas automatizadas (`tests/`, `pytest`, incluyendo pruebas
  end-to-end con `streamlit.testing.v1.AppTest`) para las reglas de negocio
  de autenticación, préstamos y resolución de códigos de barras.
- Integración continua en GitHub Actions: chequeo de sintaxis + pruebas en
  cada push/PR.
- Importación masiva de inventario por CSV, con plantilla descargable y una
  sola sincronización a GitHub por lote.
- Filtros de categoría, ubicación y tipo en el catálogo de inventario.
- Página "Mi perfil" para ver los propios datos y cambiar la contraseña.
- Gráficas de analítica (salidas por día, items activos por categoría).
- Baja en cascada de un contenedor maestro y sus items hijos, bloqueada si
  alguno tiene préstamos abiertos.
- Validación estructural de la jerarquía maestro/hijo al crear o editar
  items.

### Corregido
- **Bug de robustez crítico**: cualquier función que leyera `st.secrets`
  fallaba si no existía ningún archivo `secrets.toml`, tumbando módulos
  completos. Ahora todo el acceso a secrets pasa por `core.config.safe_secret`,
  que siempre degrada a un valor por defecto.
- **Bug crítico de navegación**: todas las vistas exponían una función
  llamada `render`, por lo que `st.navigation` generaba pathnames de URL
  duplicados e impedía iniciar sesión. Se asignó un `url_path` explícito a
  cada página.
- El historial de auditoría registraba la cantidad **absoluta** como
  `quantity_change` en cada edición; ahora registra el **delta real**.
