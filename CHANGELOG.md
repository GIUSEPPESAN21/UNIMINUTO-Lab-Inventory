# Changelog

## v1.14.0 — (en preparación)

### Restablecer contraseñas
- **Usuarios → 🔑 Restablecer contraseña** en la tarjeta de cada estudiante y
  profesor: el maestro genera una contraseña temporal (12 caracteres aleatorios en
  tres grupos, sin `0/O` ni `1/l/I`) o escribe una (con confirmación y la misma
  regla del registro: mínimo 8 caracteres). Paso de confirmación antes de aplicar;
  la temporal se muestra **una sola vez** con botón de copiar y el consejo de
  entregarla en persona. Con SMTP configurado, casilla opcional (desmarcada) para
  enviarla al correo del usuario.
- **Cambio obligatorio al ingresar:** la cuenta queda con `must_change_password` y,
  al entrar con la temporal, una pantalla pide elegir una contraseña propia (no
  puede repetir la temporal) antes de mostrar la app; la tarjeta muestra la
  pastilla «Clave temporal» hasta entonces. El resto de cuentas entra como siempre.
- **Sesiones cerradas:** la nueva columna `password_changed_at` cierra en la
  siguiente recarga las sesiones abiertas antes del cambio
  (`auth.validate_session`); el restablecimiento también levanta el bloqueo por
  intentos fallidos.
- **Política:** solo un maestro activo (rol revalidado contra la base), solo a
  estudiantes y profesores; ni la propia (se cambia en Mi perfil) ni la de otro
  maestro.
- **Auditoría** en `trace_events` (evento `password_reset`: quién, a quién, cuándo,
  modo, si se envió correo) sin la contraseña ni su hash.
- La hoja `users` suma `must_change_password` y `password_changed_at`; las bases
  anteriores se completan solas al cargar. Nuevas funciones en `core/auth.py`:
  `admin_reset_password`, `complete_forced_password_change`,
  `generate_temporary_password`, `validate_new_password`, `must_change_password`.
- Pruebas: `tests/test_password_reset.py` (unitarias) y
  `tests/test_password_reset_ui.py` (AppTest de la página Usuarios y del ingreso).

### Correos automáticos
- **Todo lo que se pide avisa por correo a los perfiles maestro:** solicitudes de
  productos y servicios, reservas, salidas (checkout) y devoluciones (checkin). El
  asunto dice qué y quién, p. ej. `[Laboratorio] Nueva solicitud: Microscopio —
  Ana Prueba (Estudiante)`, y el cuerpo indica **quién** (nombre, rol, correo, ID),
  **qué** (producto y código, cantidad, servicio, fechas) y **cuándo** (hora de
  Bogotá), con un enlace a la app si existe el secret `APP_URL`. Se envía texto
  plano más una versión HTML sencilla (multipart).
- **Respuestas al solicitante:** cuando una solicitud o reserva se aprueba o se
  rechaza, quien la pidió recibe un correo con la decisión y la observación.
- **Destinatarios:** perfiles maestro activos + `ADMIN_NOTIFICATION_EMAILS` (+ los
  profesores activos si el maestro lo activa), sin duplicados y sin correos
  inválidos o anónimos (`noreply`, `anonimo`, dominios de ejemplo).
- **Nunca bloquea ni rompe nada:** el envío ocurre en un hilo en segundo plano; si
  el servidor falla, la operación queda guardada y el error se registra. Sin SMTP
  configurado todo funciona como antes (no se envía ni se intenta nada).
- **Reportes → Correos (solo maestro):** estado del servidor SMTP (sin mostrar
  valores), guía con ejemplo de Gmail y contraseña de aplicación, un interruptor
  por tipo de aviso (todos activos por defecto, guardados en la hoja `settings`),
  lista de destinatarios, resumen de vencidos bajo demanda, **Enviar correo de
  prueba** y los últimos envíos con su estado.
- **Registro de envíos:** nueva hoja `notification_log` (se crea sola en bases
  anteriores; conserva los últimos 500). Los campos `email_notified` / `email_error`
  de solicitudes y reservas siguen funcionando.
- Mensajes de error SMTP más claros (credenciales, conexión, remitente) sin revelar
  ningún secreto.

### Fotos de los objetos
- **Foto de cada objeto con la cámara del celular**, para reconocerlo y para la
  trazabilidad: acción **📷 Foto** en las tarjetas del Inventario (última foto +
  tomar o subir otra), **galería** en el editor (más reciente primero, con fecha,
  autor, tipo y nota; el maestro puede eliminar) y, en **Escanear**, la última foto
  del objeto para todos los roles y «Foto de estado (opcional)» para profesor y
  maestro al dar salida o reingresar. Tipos: registro, estado e inventario.
- **Privacidad y peso:** se aplica la orientación EXIF, se eliminan todos los
  metadatos (GPS incluido), se reduce a 1024 px y se guarda como JPEG de calidad ~75
  (objetivo < 150 KB), con miniatura de 256 px.
- **Almacenamiento:** en el mismo repositorio privado de la base
  (`fotos/<código>/<fecha>.jpg`, API de contenidos de GitHub con los mismos secretos;
  `GITHUB_PHOTOS_DIR` opcional). Nueva hoja `item_photos` (migración automática de
  las bases existentes). Sin GitHub, las fotos quedan en el disco con una advertencia.
  Descargas con caché (`st.cache_data` + disco) y en paralelo; los errores de red no
  tumban la página.
- **Trazabilidad:** eventos `photo_added` / `photo_deleted` en la cadena de custodia.
- Nuevos `core/photos.py` y `views/photo_panel.py`; cambios aditivos en
  `core/storage.py`, `core/traceability.py`, `views/inventario.py` y
  `views/escanear.py`. Eliminar un ítem quita sus fotos del índice. Ver README
  («Fotos de los objetos») por el tamaño del repositorio.

### Chips NFC
- **Cada chip guarda una URL**, no un código suelto: `<APP_URL>/escanear?nfc=<código>&s=<firma>`.
  Al acercar el teléfono (Android, o iPhone XS en adelante) se abre la app sin instalar nada
  ni usar Web NFC. Funciona con etiquetas NTAG213, NTAG215 (recomendada) y NTAG216.
- **Prueba de presencia.** Cada toque guarda un evento `nfc_tap` (persona, código, hora) en la
  hoja `trace_events`; el mismo chip tocado dos veces en menos de un minuto cuenta una vez. Si la
  persona no ha iniciado sesión, el toque espera y se registra al entrar. La URL se limpia
  (`?nfc=` desaparece) para que recargar no lo cuente doble.
- **Ruta verificable.** Si el estudiante tiene una solicitud aprobada cuya ruta pasa por ese
  chip, el toque confirma el punto de control con el método «Chip NFC tocado» (también si lo toca
  desde otra pestaña); al completar la ruta se guarda el mismo comprobante de siempre. Un chip
  que no es de su ruta muestra hacia dónde ir.
- **Firma opcional (`NFC_SECRET`).** Con ese secreto cada URL lleva una firma corta (HMAC,
  10 caracteres) y la app rechaza las URL sin firma, alteradas o inventadas. `APP_URL` fija la
  dirección pública que se graba (si falta, se usa la dirección con la que se abrió la app).
- **Nueva página Chips NFC** (Gestión, profesor y maestro):
  - *Inventario con el teléfono*: abre un conteo (todo el inventario, una estantería o un
    contenedor); cada chip tocado, o código escrito/escaneado, marca el producto como verificado,
    con la cantidad contada opcional. Muestra el avance (se actualiza solo), lo que falta y las
    diferencias contra lo que debería haber en el estante (disponible = total − en préstamo). Al
    cerrar se guarda un resumen y los conteos anteriores quedan consultables. **Las cantidades no
    cambian al contar**: solo el maestro puede aplicar ajustes, eligiéndolos y confirmando
    explícitamente; cada ajuste queda en el historial del producto.
  - *Grabar etiquetas*: la URL de cada producto o ubicación para copiar (con su tamaño en bytes y
    los chips en que cabe), guía paso a paso con la app gratuita NFC Tools, cómo probar el chip,
    registro de chips grabados/probados/sin chip y descarga de todas las URL en CSV.
  - *Toques recientes*: quién tocó qué chip y cuándo.
- Nuevos tipos de evento en `trace_events` (sin hojas nuevas): `nfc_tap`, `nfc_tag_written`,
  `nfc_tag_removed`, `count_started`, `count_mark` y `count_closed`. El método `nfc` cuenta como
  etiqueta verificada en la ruta. El toque aparece en la cadena de custodia del producto.

### Ubicaciones con código (estanterías, pisos, mesas)
- **Nuevo tipo de ítem `location` (Ubicación)** para estanterías, pisos, mesas de trabajo,
  la exhibición Lego y zonas, cada uno con su etiqueta y código de barras. Usan la misma
  hoja `items` (sin cambios en el Excel) y no tienen stock: no se prestan, no se solicitan
  y no cuentan en el catálogo, el inicio ni los reportes.
- **Códigos que no chocan con los actuales:** `2-0-00-00-000` (estantería),
  `2-1-00-00-000` (piso), `M1-E0` (mesa), `E3-LM00` (exhibición Lego) y códigos libres
  para zonas. Son combinaciones que `parse_code` ya rechazaba, así que ningún contenedor,
  caja o producto puede tomarlos, y las ubicaciones solo aceptan estos códigos. Los
  códigos y la validación actuales no cambian.
- **Inventario → 🗺️ Ubicaciones:** mapa en árbol con lo que guarda cada ubicación, alta
  «Estantería 2 con 4 pisos» en una sola escritura (con vista previa, sin duplicar lo ya
  registrado), mesas, exhibición Lego y zonas, edición/eliminación (borrar una estantería
  borra sus pisos, no los productos) y etiquetas: individuales y un PDF con todas las de
  una estantería y sus pisos.
- **Etiqueta:** mismo formato clásico; el tipo dice «Ubicación · Piso», la ruta es
  `RUTA: E2 › P1` y el aviso «PUNTO DE CONTROL · ESCANÉALO AL LLEGAR». La etiqueta de los
  códigos actuales no cambia (el PDF es idéntico byte a byte).
- **Escanear:** al leer una ubicación se ve qué guarda y cómo llegar; si no está
  registrada, el profesor la registra ahí mismo.
- **Ruta verificable:** cuando la estantería, el piso o la mesa del camino tienen
  etiqueta registrada, ese punto se confirma escaneándola (y orienta si se escanea otro
  piso u otra estantería). Sin etiqueta registrada, la ruta funciona como antes.
- **Salud del inventario:** reglas propias para ubicaciones (código, piso dentro de su
  estantería, nombre acorde al código, sin cantidad) sin falsos avisos de categoría,
  ubicación o stock.

## v1.13.1 — La etiqueta vuelve a su formato de siempre

### Cambiado
- **Formato clásico de la etiqueta** (el de antes de v1.11, que el laboratorio
  prefiere): logo con «LABORATORIO DE INGENIERÍA», tipo de activo y aviso; filete;
  nombre en banda negra; ruta y datos; barras de 9 a 10 mm y código legible grande.
  Se conservan el tamaño real configurable, el PDF que se imprime punto por punto y
  la hoja de prueba.
- **Solo se ajusta el contenido** para que no salga cortado: el texto baja hasta su
  mínimo y luego pierde el espacio extra entre letras (el aviso completo cabe en
  50 × 25 mm); la ruta, la ubicación y la categoría pasan a una segunda línea; si no
  hay alto, se omite la categoría entera antes que partir una palabra; «…» solo como
  último recurso. Con el inventario actual las 24 etiquetas salen completas.
- Las letras vuelven a dibujarse suavizadas y umbralizadas a 1 bit: el mismo grosor
  de las etiquetas anteriores.

### Interfaz
- Letra de toda la app un poco más grande: tamaño base de 17 px en lugar de 16
  (`baseFontSize` en `.streamlit/config.toml`); todo el diseño usa rem y crece en
  proporción.

## v1.13.0 — Interfaz simétrica con la paleta de UNIMINUTO

### Colores
- Paleta institucional tomada del logotipo y del manual de imagen: azul Pantone
  287 C (`#003698`) para toda la interfaz y amarillo Pantone 116 C (`#FFCE00`)
  solo como acento. Se quitaron los brillos amarillos superpuestos al azul (daban
  un tono verde oliva), los azules ajenos a la marca y los bordes de colores de
  las tarjetas; los tonos de estado quedan solo en pastillas y avisos. Las
  gráficas de Reportes usan los mismos colores.
- Íconos monocromáticos (Material Symbols, la fuente que ya trae Streamlit) en
  lugar de emojis multicolor: navegación, encabezados, indicadores, accesos,
  pestañas, botones y avisos. Los componentes de `core/ui.py` siguen aceptando
  emojis y los traducen (`material_name`, `material`, `icon_html`).

### Simetría y centrado
- Logotipo nítido y centrado en una tarjeta blanca en la parte superior de la
  barra lateral, con el nombre del sistema debajo; la tarjeta de usuario y el
  botón de salida, centrados al pie.
- Encabezados de página centrados (ícono y título en una línea, subtítulo y
  subrayado amarillo); títulos de sección centrados entre dos filetes; inicio
  con la bienvenida centrada.
- Indicadores con el contenido centrado y filas incompletas centradas (con 5
  indicadores: 5, o 3 + 2 en tableta); accesos rápidos repartidos en filas
  parejas (7 → 4 + 3) con la segunda fila centrada y todas las tarjetas del
  mismo tamaño; la última fila de productos del inventario también se centra.
- Pestañas como control segmentado de ancho completo con pestañas iguales.
- Acceso: los dos paneles miden lo mismo y la marca va centrada.

### Compatibilidad con las dependencias actualizadas (Streamlit 1.65, pandas 3)
- **pandas 3**: `dtype=str` ahora crea columnas de texto estrictas y editar un
  ítem (cantidad, contenedor) después de reiniciar la app fallaba con
  `TypeError: Invalid value '5' for dtype 'str'`. Las hojas del Excel se cargan
  como `object`, igual que con pandas 2.
- **Streamlit 1.65** pasó sus controles a react-aria: las pestañas se
  estilizan con los nuevos selectores y los controles nativos (campos,
  opciones, casillas, menús) toman los colores del nuevo
  `.streamlit/config.toml`, con variantes `[theme.light]` y `[theme.dark]` que
  respetan la preferencia del sistema.
- Las pruebas con `AppTest.from_file` usan la ruta absoluta de `app.py` (desde
  1.65 las rutas relativas se resuelven desde la carpeta `tests/`).

### Celular
- Cabecera tipo app con el símbolo de UNIMINUTO centrado (nuevo
  `assets/uniminuto-simbolo.png`, también ícono de la pestaña del navegador).
- Indicadores y accesos rápidos en mosaico de dos columnas (el impar, centrado o
  a lo ancho), pestañas en celdas iguales que dejan saltar el texto, botones
  más altos para el dedo.

## v1.12.1 — Dependencias instalables otra vez

### Corregido
- **Pillow vuelve a 11.3.0.** La actualización a Pillow 12.3.0 dejaba los requisitos
  sin solución (Streamlit 1.50 exige `pillow<12`): el CI fallaba al instalar y un
  reinicio en Streamlit Cloud habría dejado la app caída. Dependabot ya no propone
  Pillow 12 hasta que se actualice Streamlit.
- **Contraseñas de más de 72 bytes con bcrypt 5.** bcrypt 5 las rechaza con un
  error; la app las recorta a 72 bytes como hacía bcrypt 4, así que el registro y
  el cambio de contraseña no fallan y los hashes ya guardados siguen siendo válidos.

### Verificado
- plotly 7.1.0, bcrypt 5.0.0, twilio 9.11.2, pytest-cov 7.1.0 y las acciones
  `checkout`/`setup-python` v7: la suite completa pasa y los gráficos de Reportes se
  ven bien.

## v1.12.0 — Interfaz propia, productos desde la descripción y trazabilidad

### Nuevo
- **🧭 Trazabilidad:** ruta de retiro verificable para estudiantes (cada etiqueta del
  camino se confirma escaneándola; si es la equivocada, la app indica dónde está y
  hacia dónde ir), comprobante corto al completarla y línea de tiempo de cada
  solicitud. Profesor y maestro: buscador, validación del comprobante, cadena de
  custodia por producto y avance de servicios (en curso / entregado). Nueva hoja
  `trace_events`; recorrer la ruta no escribe, se guarda un solo evento al final.
- **✨ Generar productos desde la descripción** de un Contenedor Principal: propuesta
  editable de Contenedores de Característica con código consecutivo, ubicación
  heredada y avisos de ambigüedad; alta masiva con una sola escritura
  (`save_items_bulk`). Catálogo agrupado por contenedor con tarjetas y filtros de
  stock.
- **🩺 Salud del inventario** en Reportes: auditoría de códigos, jerarquía,
  ubicaciones, errores de digitación, categorías, medidas imposibles y contenido sin
  registrar, con correcciones seguras confirmadas antes de escribir.

### Interfaz
- Sistema de diseño propio: paleta institucional, botones con degradado y
  animación, pestañas tipo pastilla, tarjetas, indicadores, líneas de tiempo y modo
  oscuro coherente; adaptado a celular y respetuoso de «reducir movimiento».
- Barra lateral con marca, página activa resaltada y tarjeta de usuario; inicio con
  saludo, indicadores y accesos rápidos según el rol; nueva pantalla de acceso.
- Reportes, Préstamos, Reservas, Usuarios, Perfil y Acerca de usan los mismos
  componentes; gráficas sin fondo propio para el modo oscuro.

### Corregido
- Reservas fallaba con «multiple elements with the same key» cuando un revisor tenía
  una reserva propia aprobada.
- Botones primarios con ayuda emergente y botones de formulario salían en el rojo
  por defecto de Streamlit.

### Calidad
- De 562 a 863 pruebas (cobertura 89 %), en verde con Python 3.11, 3.12 y 3.13.

## v1.11.0 — Etiquetas legibles, a tamaño real y sin letras apretadas

### Corregido
- **Letras diminutas y pegadas en la etiqueta:** había textos de 3,5–4,3 pt, 1 punto
  (0,12 mm) entre líneas y el aviso siempre salía truncado («NO RETIRAR SIN P…»). La
  causa era un diseño fijo de 384 × 192 puntos que forzaba barras de 9 mm en 25 mm de
  alto y encogía todo lo demás. Ahora ningún texto baja de ~5 pt (nombre ≥ 7 pt), hay
  espacio visible entre todos los bloques y las letras llevan espaciado proporcional.
- **Etiqueta que se imprimía pequeña:** el PDF medía siempre 50 × 25 mm, así que en un
  rollo o papel de driver más grande (la SAT TT460 acepta etiquetas de 20 a 112 mm de
  ancho) salía reducida en una esquina. Ahora el tamaño se configura y el PDF mide
  exactamente eso, ocupando toda la página (antes quedaba ~1 mm sin usar).
- **Barras y letras remuestreadas al imprimir el PDF:** el raster iba centrado en la
  página, a una fracción de punto de los puntos del cabezal. Chrome y Edge, con la
  escala Predeterminado, reescalaban la etiqueta de 50 × 25 mm y perdían una fila de
  puntos, y los visores basados en poppler deformaban las barras en todos los
  tamaños. Ahora el raster se ancla arriba a la izquierda, como alinea Chrome al
  imprimir, y sus bordes caen apenas dentro de puntos enteros, de modo que todos esos
  visores lo copian punto por punto.
- **Etiqueta recortada, ampliada y borrosa al imprimirla desde la app Fotos de Windows:**
  el PNG se abría en Fotos, cuyo cuadro de impresión usa el papel «USER (50,8 × 50,8 mm)»
  del driver y *Rellenar página*: recortaba el centro de la imagen, la agrandaba ×2,1 y
  las barras de 2 puntos salían de 4 y de 5. Ahora el botón dice «PNG · solo archivo»
  (con la advertencia), y la pestaña indica el **papel que debe definirse en el driver**
  (en mm y en pulgadas) y trae una guía paso a paso para Edge/Chrome y una tabla de
  síntomas (recortada, pequeña, girada, borrosa, marco cortado, desfase).
- Las métricas de la pestaña usan coma decimal, y los botones deshabilitados se ven
  deshabilitados en toda la app (antes un botón primario deshabilitado seguía azul).

### Nuevo
- **Inventario → 🖨️ Etiquetas** (profesor y maestro):
  - Tamaño del rollo: 50 × 25, 50 × 30, 60 × 40, 100 × 50, 100 × 75, 100 × 100,
    100 × 150 mm o personalizado (20–104 × 15–160 mm, así caben 4 × 3 y 4 × 6 pulgadas).
  - Resolución: 203 dpi (SAT TT460) o 300 dpi.
  - Contenido opcional: logo y laboratorio, aviso, ruta, ubicación, tipo y categoría.
  - Vista previa en vivo con cualquier ítem del inventario, métricas de legibilidad
    y aviso de lo que no cabe.
  - Guía de impresión paso a paso.
  - La configuración se guarda en la nueva hoja `settings` de la base y la usan
    Inventario, Escanear y la vista previa del registro.
- **Hoja de prueba de impresión:** PDF del tamaño configurado con un marco a 1 mm del
  borde, una regla milimetrada y tres rejillas de barras (0,25, 0,35 y 0,5 mm, los
  módulos del Code 128). Medirla indica si el visor reduce la página (regla más
  corta), si el rollo es más grande (la prueba ocupa solo una parte), si el papel del
  driver no coincide (marco cortado) o si la imagen se remuestrea (rejillas desiguales
  o grises).
- **PDF listo para imprimir a tamaño real:** `/PrintScaling /None`,
  `/PickTrayByPDFSize`, versión 1.7 y título con el tamaño.

### Diseño adaptable de la etiqueta
- Composición por prioridades: si todo no cabe a tamaño legible, se omite lo menos
  importante (categoría → tipo → aviso → ubicación…) en vez de apretar. La marca
  institucional, el nombre, el Code 128 y el código siempre se conservan.
- En etiquetas pequeñas la ruta sube junto al logo y ahorra una fila; el aviso usa su
  versión corta antes que recortarse; un nombre largo pasa a dos líneas cuando hay
  espacio. El código y la ruta nunca se recortan.
- En etiquetas grandes el espacio extra se usa para mostrar más datos y barras más
  altas (hasta el 36 % del alto, máximo 18 mm). Antes solo crecía el tamaño de letra.
- Rasterizado monocromo con *hinting*: trazos uniformes y letras separadas, sin el
  empaste que producía suavizar y luego umbralizar a 1 bit.
- Barras con módulo entero de 0,25–0,5 mm, zona de silencio de 10 módulos cuando cabe
  y un mínimo de 7 mm de alto (antes 9 mm fijos), por encima de la recomendación
  general para Code 128.
- Separador «|» entre datos: la ubicación libre ya puede contener «·».
- Etiquetas cacheadas por ítem y tamaño: unos 15 ms por etiqueta y el catálogo no las
  regenera en cada recarga.

### Calidad
- De 332 a 562 pruebas (cobertura 70 % → 75 %; `labels.py` 94 %), en verde con
  Python 3.11, 3.12 y 3.13:
  - Decodificación exacta del Code 128 desde los píxeles en todos los tamaños, a 203 y
    a 300 dpi.
  - Bloques que nunca se tocan, con filas en blanco comprobadas en la imagen.
  - Piso de legibilidad, textos institucionales completos y omisión priorizada.
  - PDF a tamaño exacto y regla de calibración que mide lo que dice (±0,4 mm).
  - Impresión punto por punto con el motor PDF de Chrome y Edge (PDFium), siguiendo
    su ruta de impresión en Windows, y con poppler; además, un barrido de todos los
    tamaños permitidos contra el redondeo de cada visor (`pypdfium2` se suma a las
    dependencias de desarrollo).
  - Persistencia de la configuración, migración de bases sin la hoja `settings` y
    la pestaña Etiquetas con la app real.
- Las protecciones clave se verificaron rompiéndolas a propósito (mutación).

## v1.10.0 — Eliminación definitiva, integridad de datos y seguridad

### Corregido
- **Un código eliminado no se podía volver a crear** ("ya existe un item con ese
  código"): "eliminar" solo marcaba el ítem como `retired` y su fila seguía en el
  Excel. Ahora **Eliminar definitivamente** borra el ítem, su historial y sus
  préstamos devueltos; los contenidos de un Contenedor Principal se eliminan en
  cascada, las solicitudes abiertas se cancelan y se bloquea si hay préstamos
  abiertos. Los códigos de ítems ya dados de baja también quedan libres.
- **Editar un ítem tras reiniciar la app fallaba** (`'float' object has no attribute
  'strip'`): al releer el Excel las celdas vacías llegaban como `NaN`. Ahora se leen
  como cadenas vacías, y textos como `NA`/`None` ya no se convierten en nulos.
- **Un arranque sin red podía publicar una base vacía sobre la real** en GitHub: ya
  no se sobrescribe una versión remota que la app no haya descargado.
- Una fecha límite de devolución vencía a las 7 p. m. del día anterior (medianoche
  UTC); ahora vence al terminar ese día en Colombia.
- Escanear un código dado de baja ofrece registrarlo de nuevo en vez de un callejón
  sin salida.

### Integridad de datos
- Escritura **atómica** del Excel (archivo temporal + `os.replace`).
- Publicación en GitHub **síncrona y serializada**: el cambio ya está publicado
  cuando termina la operación; varias escrituras simultáneas se agrupan y siempre
  gana la última versión (antes, hilos sin orden podían subir una versión vieja o un
  archivo a medias). Reintentos ante 429/5xx y fallos de red.
- **Detección de conflictos:** se recuerda el SHA de la última sincronización; si
  GitHub tiene otra versión se rechaza el push y el maestro elige qué conservar.
  Botón de reintento de sincronización.
- Stock y cruces de reservas se **re-verifican dentro del lock** (no se puede sacar
  la misma última unidad ni aprobar dos reservas que se cruzan).
- Lecturas por hoja y disponibilidad en lote (`get_availability_map`): menos copias y
  sin una consulta por ítem al listar el catálogo o el tablero de inicio.
- `firestore_retry` pasa a llamarse `with_retry` (alias conservado) y ya no reintenta
  errores de programación.

### Seguridad
- **Rol profesor verificado:** estar en la lista blanca ya no basta; hay que escribir
  un código enviado al correo. Sin SMTP la cuenta nace estudiante.
- Mensaje de login genérico, bloqueo temporal por intentos y comparación en tiempo
  constante contra cuentas inexistentes.
- Sesión revalidada en cada recarga (rol/estado al instante), con expiración y sin
  `password_hash` en `session_state`.
- Guardas de rol dentro de Inventario, Reportes y Usuarios; escape de HTML en nombres
  y encabezados.
- Alerta de WhatsApp en segundo plano (no retrasa la interfaz).

### Calidad
- **Pruebas herméticas:** aislamiento automático del disco, de GitHub y de los
  Secrets; ya no dejan archivos ni fallan en la segunda corrida.
- De 229 a más de 330 pruebas (cobertura 53 % → 70 %): sincronización con GitHub
  simulado, concurrencia, eliminación, seguridad, `AppTest` de extremo a extremo y
  reportes.
- CI con matriz Python 3.11/3.12, lint (`ruff`) y umbral de cobertura; Dependabot.

## v1.9.2 — Títulos legibles y guía física en la etiqueta

- Mayor separación entre letras y palabras en el título institucional, tipo de activo y aviso, con medición previa para evitar recortes.
- Nombre del producto destacado en una banda negra con texto blanco y espaciado propio, sin crear una fila adicional.
- Ruta compacta derivada del código (`E`, `P`, `C`, `CJ`, `I`), omitiendo niveles `00/000` que no aplican; también cubre Mesa y Lego.
- La guía es solo visual: el Code 128 y su texto humano continúan representando exactamente el identificador original.
- Vista previa actualizada para mostrar la banda y la ruta antes de registrar.
- Se mantienen barras de al menos 9 mm y el código humano debajo del Code 128.

## v1.9.1 — Tipografía térmica y PDF a tamaño real

- Tipografía reforzada para 203 dpi: encabezado, tipo, aviso, nombre, metadatos y código humano con tamaños mínimos mayores; textos secundarios en negrita y título institucional con espaciado adicional.
- Logo más visible y distribución vertical reajustada sin reducir las barras por debajo de 9 mm.
- La vista previa deja de ampliar el PNG al ancho del navegador y elimina el texto ficticio “Vista previa”.
- Nuevo PDF de una página exacta 50×25 mm, con el raster 384×192 centrado a 203 dpi, para evitar la reducción observada al imprimir el PNG desde el visor.
- Inventario y Escanear conservan el PNG y ofrecen el PDF como opción recomendada.
- Pruebas de tamaños tipográficos, estructura PDF, MediaBox, centrado, resolución efectiva y validez de la tabla xref.

## v1.9.0 — Etiquetas profesionales con identidad institucional

- Nuevo encabezado monocromático con el logotipo oficial de UNIMINUTO, Laboratorio de Ingeniería, tipo de activo y advertencia institucional.
- El cuerpo organiza nombre, categoría y ubicación del producto; los campos opcionales ausentes se omiten de forma segura.
- El logo queda versionado dentro del repositorio y dispone de un wordmark de respaldo si el recurso no puede abrirse.
- Se conservan 50×25 mm, 384×192 px, 203 dpi, impresión al 100 %, Code 128 exacto, módulos enteros, zonas de silencio y barras de al menos 9 mm.
- Pruebas ampliadas para recurso de marca, estructura visual, separador, propagación de metadatos, DPI y decodificación exacta desde los píxeles.

## v1.8.0 — Reservas, solicitudes, correo y guía móvil

- Reservas de actividad o laboratorio completo con zona `America/Bogota`,
  validación temporal, conflictos, aprobación y cancelación por roles.
- Solicitudes de productos y servicios con validación de stock al crear y
  aprobar, estados gestionables y conservación del checkout como cierre de
  cadena de custodia.
- Correo SMTP opcional a destinatarios configurados o profesores/maestros
  activos. Un fallo de correo no pierde la solicitud.
- Guía móvil interactiva por pasos desde códigos GLIOPS, Mesa o Lego, con
  respaldo en la ubicación textual para códigos libres y heredados.
- Edición validada de nombre, ID, correo y programa por el maestro; correo
  único y actualización segura limitada a campos conocidos.
- Nuevas hojas `reservations` y `service_requests`, creadas automáticamente
  sin migración manual del Excel existente.
- Nuevos módulos de dominio, componentes reutilizables, navegación y pruebas
  unitarias/integración para las funciones anteriores.

## v1.7.0 — Generador asistido y niveles `no aplica` en códigos GLIOPS

- El estándar de cinco niveles acepta ceros finales como marcadores de
  estructura: `2-1-01-00-000` (contenedor), `2-1-01-01-000` (caja o
  subcontenedor) y `2-1-01-01-001` (ítem). Contenedor sigue siendo positivo
  y se rechaza un ítem positivo cuando Caja es `00`.
- `core/barcode.py` incorpora constructores puros y tipados para estándar,
  mesa, Lego, numérico libre y prefijo alfanumérico. La salida estándar usa
  relleno canónico 2/2/3; los prefijos generados se normalizan a mayúsculas.
- Nuevo componente compartido `views/code_input.py`: generación asistida por
  defecto, modo manual/escaneado, vista previa en vivo, validación inmediata,
  aviso de duplicados y vista previa opcional de la etiqueta.
- Inventario y Escanear reutilizan el mismo componente. El almacenamiento
  conserva su validación final y el modo manual sigue disponible para códigos
  externos o ya impresos.
- Pruebas añadidas para el caso original `2-1-01-00-000`, niveles válidos e
  inválidos, constructores, relleno, persistencia en Excel y lectura exacta de
  la etiqueta Code 128.

## v1.6.0 — Códigos numéricos y alfanuméricos libres + etiqueta con descripción

- **Códigos libres** (`core/barcode.py`): además de los 3 formatos GLIOPS
  V3, un item nuevo acepta un código numérico puro de hasta 20 dígitos
  (ej. `0012345`, con sus ceros a la izquierda) o uno alfanumérico de hasta
  13 caracteres con letras sin tildes, números y `-` `_` `.` (ej.
  `LAB-MIC-01`). Se guarda exactamente como se escribe. Los códigos con la
  forma de GLIOPS que no cumplen sus reglas se siguen rechazando.
- `CAJA-001` y códigos parecidos de la nomenclatura anterior **ahora son
  válidos** como alfanuméricos libres.
- **Etiqueta rediseñada** (`core/labels.py`): aviso institucional, nombre
  del item, código de barras Code128 y código legible en 50×25 mm. El
  Code128 se codifica en la app (subconjuntos B y C) y se dibuja con
  módulos de ancho entero en puntos de impresora; de python-barcode solo se
  usa la tabla de patrones, porque su codificador 0.16.1 pierde un `99`
  inicial. El PNG declara 203 dpi para imprimirse a tamaño real.
- Alta de items: se ignoran los espacios alrededor del código y el error de
  formato se muestra antes de guardar. La importación CSV explica cómo no
  perder los ceros a la izquierda al editar en Excel.
- Pruebas: formatos con letras, lectura del código de barras desde los
  píxeles de la etiqueta (devuelve el texto exacto) y guardado/recarga real
  en Excel de `0012345` y `LAB-MIC-01`.

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
