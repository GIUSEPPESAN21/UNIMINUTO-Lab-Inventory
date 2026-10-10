# Inventario de Laboratorio UNIMINUTO

![CI](https://github.com/GIUSEPPESAN21/UNIMINUTO-Lab-Inventory/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/badge/license-MIT-blue)

Sistema de gestión de inventario y préstamos (salida/reingreso) para el
**laboratorio de ingeniería de UNIMINUTO**, con códigos de barras jerárquicos
(Contenedor Principal + Contenedores de Característica) y control de acceso
por roles usando el correo institucional (estudiante / profesor / perfil
maestro).

Este repositorio es **público**; la base de datos (Excel con inventario,
préstamos y usuarios) vive en un repositorio **privado** aparte:
`GIUSEPPESAN21/UNIMINUTO-Lab-Database`. El código nunca contiene datos reales
ni credenciales — ambos se inyectan en tiempo de ejecución vía Secrets de
Streamlit (ver sección de Configuración). La base de datos se entrega
**vacía**: cada laboratorio carga su propio catálogo (manualmente o por CSV).

## Conceptos clave

- **Contenedor Principal** (`master`): un código de barras pegado a una caja,
  gabinete o kit que agrupa varios productos. No tiene cantidad propia.
- **Contenedor de Característica** (`child`): un código de barras individual
  para una subdivisión específica DENTRO de un Contenedor Principal (ej.
  "Resistencias 220 Ω", "Tornillos M4"); `parent_id` apunta al Contenedor
  Principal.
- **Ítem Individual** (`standalone`): un producto con código propio que no
  pertenece a ningún Contenedor Principal.
- **Ubicación** (`location`): una estantería, un piso, una mesa de trabajo o una zona con
  su propia etiqueta y código de barras (ver *Ubicaciones con código*). No tiene stock: no
  se presta ni se solicita.
- **Disponibilidad en vivo**: `disponible = cantidad_total - préstamos abiertos`.
  La cantidad total solo cambia por alta/ajuste/baja; cada salida y reingreso
  queda registrado en el libro mayor de préstamos (`loans`), nunca se resta
  directamente.

## Nomenclatura de códigos de inventario (GLIOPS V3)

Al dar de alta un item nuevo (formulario, escaneo o CSV masivo), su código
debe cumplir uno de estos formatos — validados por regex en
`core/barcode.py`. Los items ya cargados con una nomenclatura anterior
siguen funcionando con normalidad (solo se valida al crear, no al editar).

| Formato | Patrón | Ejemplo | Uso |
|---|---|---|---|
| Estándar (5 niveles) | `[ESTANTERIA 1-3]-[PISO 1-6]-[CONTENEDOR]-[CAJA]-[ITEM]` | `2-1-01-00-000` | Ubicación jerárquica: contenedor, caja/subcontenedor o ítem |
| Mesas de trabajo | `M1-E[n]` o `M2-E[n]` | `M1-E2` | Equipos de alto valor (impresora 3D, cortadora láser...) |
| Exhibición Lego | `E3-LM[n]` | `E3-LM07` | Modelos armados en la Estantería 3 |
| Numérico libre | Solo dígitos, hasta 20 | `0012345` | Equipos que ya traen su propio código numérico |
| Alfanumérico libre | Letras sin tildes, números y `-` `_` `.` entre ellos, con al menos una letra, hasta 13 caracteres | `LAB-MIC-01` | Códigos propios del laboratorio |

En el formato estándar los niveles finales que no aplican se representan con
ceros, siempre de derecha a izquierda:

| Nivel representado | Código canónico | Significado |
|---|---|---|
| Contenedor Principal | `2-1-01-00-000` | Caja e ítem no aplican |
| Caja / Contenedor de Característica | `2-1-01-01-000` | Ítem no aplica |
| Ítem Individual | `2-1-01-01-001` | Todos los niveles aplican |

El contenedor siempre debe ser positivo y no se acepta un ítem positivo si la
caja es `00`. Los valores `00`/`000` son marcadores de estructura, no cantidades.

- El modo manual guarda el código como **texto, exactamente como se escribe**:
  conserva ceros a la izquierda y mayúsculas/minúsculas.
- Un código con la *forma* de un formato GLIOPS que no cumple sus reglas
  (`4-2-05-12-001`, `2-1-01-00-001`, `M3-E1`, `m1-e2`) se rechaza.
- Los límites de longitud garantizan que un código libre quepa en la etiqueta.

### Generador asistido de códigos

El alta desde Inventario ofrece por defecto **Generar automáticamente** y
mantiene **Escanear o escribir** para códigos preexistentes. El generador:

- construye el estándar GLIOPS según el tipo de ítem, con relleno canónico
  `2/2/3` y ceros `no aplica` donde corresponde;
- genera formatos Mesa, Lego, numérico libre o `PREFIJO-NÚMERO`;
- normaliza a mayúsculas los prefijos generados, muestra el código final en
  vivo, advierte duplicados o una discrepancia de nivel y permite previsualizar
  la etiqueta;
- se reutiliza al registrar un código escaneado que todavía no existe.

La validación de `core/storage.py` sigue siendo la autoridad final antes de
guardar. La generación no reserva automáticamente "el siguiente número": el
usuario suministra la numeración para evitar colisiones con el backend actual
Excel + GitHub.

### Etiquetas imprimibles

Desde Inventario o Escanear, el menú **🏷️ Etiqueta** ofrece el **PDF**, con una página
que mide exactamente lo mismo que la etiqueta, y un **PNG solo para guardar o integrar**:
**no se imprime** (ver «Cómo imprimir bien»).

**Tamaño real configurable.** En **Inventario → 🖨️ Etiquetas** el laboratorio elige
el tamaño de su rollo (50 × 25, 50 × 30, 60 × 40, 100 × 50, 100 × 75, 100 × 100,
100 × 150 mm o uno personalizado de 20–104 × 15–160 mm; también 4 × 3 o 4 × 6 pulgadas
con 101,6 × 76,2 o 101,6 × 152,4) y la resolución de la impresora (203 dpi
para la SAT TT460, o 300 dpi). La configuración se guarda en la base de datos y la
usan todas las etiquetas de la app. Cada pixel del raster es un punto del cabezal y
el PDF le pide al visor imprimir sin escalar (`/PrintScaling /None`) y elegir el papel
por el tamaño del documento (`/PickTrayByPDFSize`). El raster se ancla arriba a la
izquierda, con los bordes alineados a puntos enteros de la impresora, así que al
imprimir a tamaño real se copia punto por punto, sin remuestrear las barras. Las
pruebas lo verifican con el motor PDF de Chrome y Edge (PDFium), siguiendo su ruta de
impresión en Windows, y con poppler.

**Formato clásico, contenido ajustado.** La etiqueta conserva el formato de siempre
del laboratorio —logo con «LABORATORIO DE INGENIERÍA», tipo de activo y aviso; filete;
nombre en banda negra; ruta y datos; Code 128 de 9 a 10 mm y código legible grande—,
escalado al tamaño elegido. Solo se ajusta el contenido para que nada salga cortado:

- Un texto que no cabe baja de tamaño hasta su mínimo; si aun así no cabe, pierde el
  espacio extra entre letras (así el aviso completo «ACTIVO INSTITUCIONAL · NO RETIRAR
  SIN PRÉSTAMO» cabe en 50 × 25 mm).
- La ruta, la ubicación y la categoría pasan a una segunda línea antes que cortarse;
  si no hay alto para dos líneas, se omite la categoría (y luego la ubicación) entera,
  sin partir palabras. Solo como último recurso un texto se acorta con «…».
- Ningún texto baja de 9 puntos de impresora a 203 dpi (~3,2 pt), el mínimo del
  formato clásico; el código y la ruta nunca se recortan. La pestaña Etiquetas muestra
  la vista previa y dice qué se omitió; cada contenido se puede activar o desactivar.
- Las letras se dibujan suavizadas y luego se pasan a 1 bit, con el mismo grosor de
  las etiquetas que el laboratorio ya usa.

La etiqueta puede incluir el logotipo de UNIMINUTO, Laboratorio de Ingeniería, el aviso
institucional, el nombre en una banda negra de alto contraste, la ruta física, la
ubicación, el tipo, la categoría, el Code 128 y el código legible. La ruta compacta se
deriva del código —por ejemplo, `RUTA: E2 › P1 › C01 › CJ02 › I003`, donde `E`, `P`,
`C`, `CJ` e `I` significan estantería, piso, contenedor, caja e ítem— y omite los
niveles `00/000` que no aplican sin alterar el valor codificado. En etiquetas pequeñas
sube junto al logo para ahorrar una fila. Mesa y Lego generan su ruta equivalente; los
códigos libres usan la ubicación textual.

Las barras usan un número entero de puntos por módulo (0,25–0,5 mm), zona de silencio
de 10 módulos cuando cabe (nunca menos de 6) y al menos 7 mm de alto, por encima de la
recomendación general para Code 128 (≥ 6,35 mm o el 15 % del ancho del símbolo). En las
etiquetas más altas crecen hasta el 36 % del alto.

### Ubicaciones con código (estanterías, pisos, mesas)

En el menú lateral **Gestión → Ubicaciones** (y también en **Inventario → 🗺️ Ubicaciones**; solo profesor y maestro) cada estantería, piso, mesa de trabajo o zona tiene
su propia etiqueta con código de barras, para pegarla en el lugar y escanearla. Son ítems
de tipo `location` en la misma hoja `items` (no cambia el esquema del Excel).

| Ubicación | Código | Notas |
|---|---|---|
| Estantería 2 | `2-0-00-00-000` | Piso `0` = la estantería misma |
| Piso 1 de la Estantería 2 | `2-1-00-00-000` | Su `parent_id` es la estantería |
| Mesa de trabajo 1 | `M1-E0` | Equipo `0` = la mesa misma |
| Exhibición Lego | `E3-LM00` | Modelo `00` = la exhibición; dentro de la Estantería 3 |
| Zona, sala o subnivel | libre, p. ej. `SALA-A` | Numérico o alfanumérico (hasta 13 caracteres) |

- **Sin colisiones:** estos códigos reutilizan el formato GLIOPS con ceros en niveles que
  el formato de inventario rechaza (piso 0, contenedor 00, equipo 0, modelo 00), así que
  `parse_code` los sigue rechazando para contenedores, cajas y productos, y las
  ubicaciones solo aceptan sus propios códigos (`validate_location_code`). Los códigos de
  contenedores (`2-1-01-00-000`), cajas, ítems, `M1-E2` y `E3-LM07` no cambian.
- **Sin stock:** cantidad siempre 0; no aparecen en el catálogo de productos, el inicio,
  los reportes ni las solicitudes, y no se les da salida.
- **Crear en bloque:** «Estantería 2 con 4 pisos» crea la estantería y sus pisos en una
  sola escritura (`save_items_bulk`), con vista previa; lo ya registrado se muestra como
  tal y solo se agrega lo que falta. Mesas, la exhibición Lego y zonas se registran una a
  una; un piso solo puede estar dentro de su estantería.
- **Mapa y etiquetas:** árbol de ubicaciones con lo que guarda cada una (contenedores y
  productos, por su código GLIOPS o por la ubicación escrita si tienen código libre),
  etiqueta individual y un PDF con todas las etiquetas de una estantería y sus pisos. La
  etiqueta mantiene el formato clásico: el tipo dice «Ubicación · Piso», la ruta es
  `RUTA: E2 › P1` y el aviso es «PUNTO DE CONTROL · ESCANÉALO AL LLEGAR».
- **Escanear:** leer la etiqueta de una ubicación muestra qué guarda y cómo llegar; si
  todavía no está registrada, el profesor puede registrarla ahí mismo.
- **Ruta verificable:** si la estantería, el piso o la mesa del camino están registrados,
  esos puntos pasan de «Sin etiqueta: confírmalo al llegar» a confirmarse escaneando su
  etiqueta (entra en el comprobante). Si no están registrados, la ruta es la de siempre.
- **Salud del inventario:** las ubicaciones tienen sus propias reglas (código de
  ubicación, piso dentro de su estantería, nombre acorde al código) y no se marcan por
  falta de categoría, ubicación escrita o cantidad.

### Cómo imprimir bien (Windows, SAT TT460 y Edge o Chrome)

La etiqueta sale exacta cuando **la medida es la misma en cuatro lugares**: el rollo, la
pestaña **🖨️ Etiquetas**, el papel del driver y el papel del cuadro de impresión.

1. **Mide el rollo** (ancho × alto, sin el papel de soporte), elígelo en **Inventario →
   🖨️ Etiquetas** y guarda. La pestaña indica el papel que debe usar el driver.
2. **Driver de la SAT TT460** (*Preferencias de impresión*, y también *Propiedades de la
   impresora → Opciones avanzadas → Valores predeterminados de impresión*):
   - *Configuración de página*: tamaño **definido por el usuario** (en el cuadro de
     impresión aparece como «USER») con esa medida. Si el cuadro muestra
     «USER (50,8 × 50,8 mm)», ese es el valor que hay que cambiar.
   - *Papel / Stock*: etiquetas con separación (no continuo).
   - *Gráficos*: 203 dpi y tramado (*Dithering*) en **Ninguno**.
   - *Opciones*: velocidad y oscuridad medias.
3. **Imprime el PDF desde Edge o Chrome** (*Abrir con → Microsoft Edge*, `Ctrl + P`):
   papel **USER** con tu medida, escala **Predeterminado** (o **Tamaño real**, si
   aparece) y, si el cuadro los muestra, sin márgenes ni encabezados. La vista previa
   debe mostrar la etiqueta completa, derecha y llenando el papel.
4. **No imprimas el PNG ni abras la etiqueta con la app Fotos.** Fotos usa
   *Rellenar página*: recorta el centro de la imagen y la agranda con un factor que no es
   entero, así que el texto sale grueso y las barras irregulares.
5. **Una etiqueta de prueba antes del lote:** **🖨️ Etiquetas → Hoja de prueba de
   impresión** descarga un PDF con un marco a 1 mm del borde, una regla milimetrada y tres
   rejillas de barras. Debe verse el marco completo, la regla debe medir lo indicado y las
   rejillas deben verse parejas (con una lupa o la cámara del celular).

| Lo que ves | Causa más probable | Qué hacer |
|---|---|---|
| Recortada y ampliada | PNG impreso desde Fotos con *Rellenar página*, o papel del driver menor que la imagen | PDF desde Edge o Chrome con el papel USER de la medida real |
| Pequeña, en el centro o en una esquina | El papel del driver o la medida guardada no es la del rollo | La misma medida en la app y en el driver |
| Girada 90° | Orientación | Cambiar *Diseño* hasta ver el texto derecho |
| Borrosa o con barras desiguales | La imagen se reescaló (Fotos, *Ajustar*, escala ≠ 100 %) o el driver aplica tramado | PDF, escala Predeterminado y *Dithering: Ninguno* |
| Marco cortado en un lado | Papel del driver menor que la etiqueta, o rollo corrido | Revisar el papel y centrar las guías del rollo |
| Se desfasa de una etiqueta a otra | Sensor sin calibrar o tipo de papel equivocado | Calibrar el sensor y elegir etiquetas con separación |

La misma guía está en la pestaña **🖨️ Etiquetas**.

## Operación del laboratorio

### Reservas

Todo usuario autenticado puede solicitar una actividad o el laboratorio
completo. Las fechas se interpretan en `America/Bogota` y se guardan en UTC.
Una reserva completa entra en conflicto con cualquier reserva aprobada que se
solape; dos actividades distintas pueden coexistir, pero dos reservas de la
misma actividad no. Profesor y maestro aprueban/rechazan; el solicitante puede
cancelar. No se imponen horarios de apertura porque la documentación no los
define.

### Solicitudes de productos y servicios

Los productos validan existencia, estado y disponibilidad tanto al solicitar
como al aprobar. Aprobar no descuenta inventario: la salida se confirma en
Escanear para conservar la cadena de custodia. Los servicios requieren nombre,
descripción y fecha. Si SMTP está configurado, los administradores reciben un
correo; si falla, la solicitud permanece guardada y muestra la advertencia.

### Ruta verificable y trazabilidad (🧭 Trazabilidad)

Cada producto muestra su ruta derivada del código V3 (estantería › piso ›
contenedor › caja › ítem) con la etiqueta que se encontrará en cada punto. Mesa y
Lego tienen rutas especiales; códigos libres/heredados usan `location`.

- **Estudiante:** con una solicitud aprobada, recorre la ruta paso a paso y
  confirma cada punto **escaneando su etiqueta** (o escribiendo el código). Si
  escanea una equivocada, la app le dice dónde está y hacia dónde ir. Estantería y
  piso se confirman al llegar o quedan probados al escanear el contenedor; si tienen
  etiqueta registrada en **Inventario → Ubicaciones**, se confirman escaneándola. Al terminar recibe un **comprobante** corto y ve su solicitud en
  una línea de tiempo (creada → revisada → ruta verificada → retirada → devuelta).
  Los servicios muestran creada → aprobada → en curso → entregado.
- **Profesor/maestro:** busca por solicitud, producto, estudiante o comprobante;
  ve quién hizo qué y cuándo, valida el comprobante con el producto en la mano,
  consulta la **cadena de custodia** de un producto y marca el avance de los
  servicios.
- Recorrer la ruta no escribe en la base: se guarda **un solo evento** al
  completarla (hoja `trace_events`). Registrar la salida en Escanear enlaza el
  préstamo con la solicitud aprobada. Un estudiante solo ve sus propios registros.

### Fotos de los objetos (📷)

Cada objeto (contenedor, caja o ítem) puede tener fotos tomadas con la cámara del
celular, para reconocerlo y dejar constancia de su estado en la cadena de custodia.

- **Dónde:** en **📦 Inventario**, cada tarjeta tiene una acción **📷 Foto** (se
  abre sin recargar el catálogo: muestra la última foto y permite tomar o subir
  otra); en **✏️ Editar** hay una **galería** (la más reciente primero, con fecha,
  autor, tipo y nota; el **maestro** puede eliminar). En **Escanear**, al encontrar
  un objeto se muestra su última foto a cualquier rol (ayuda a reconocerlo) y
  profesor/maestro pueden agregar una **foto de estado** al dar salida o reingresar
  (opcional). Agregan fotos profesor y maestro; el estudiante solo las ve.
- **Tipos:** `registro` (al dar de alta), `estado` (salida/reingreso) e
  `inventario` (conteo). Se puede escribir una nota.
- **Cámara:** `st.camera_input` (interruptor «Usar la cámara aquí») o subir un
  JPG/PNG; en el celular, «Subir foto» también abre la cámara.
- **Privacidad y peso:** antes de guardar se aplica la orientación de la cámara,
  se **eliminan todos los metadatos** (EXIF, ubicación GPS, modelo del teléfono,
  comentarios), se reduce a **1024 px** de lado largo y se guarda como JPEG de
  calidad ~75 (normalmente 30–120 KB; si una foto pesa más de 150 KB se baja la
  calidad/resolución). La miniatura de 256 px se genera al mostrarla y no se guarda.
- **Dónde se guardan:** en el **mismo repositorio privado** de la base de datos,
  en `fotos/<código>/<AAAAMMDDTHHMMSSZ>-<azar>.jpg` (API de contenidos de GitHub,
  mismos `GITHUB_TOKEN`/`GITHUB_REPO`; carpeta configurable con `GITHUB_PHOTOS_DIR`).
  El índice está en la hoja `item_photos` (`id, item_id, path, sha, taken_by,
  taken_at, kind, note`) del Excel. Subir una foto = un commit de la imagen + una
  escritura de la base (índice y evento juntos). Si la subida falla no se registra
  nada y se puede reintentar; **sin GitHub** las fotos quedan en el disco del
  servidor con una advertencia (se pierden al reiniciar, como la base).
- **Lectura:** cada foto se descarga de GitHub una sola vez por servidor (caché de
  Streamlit + copia en disco); las galerías descargan en paralelo y las miniaturas
  salen de esa copia. El contenido de **📷 Foto** solo se ejecuta si está abierto.
- **Trazabilidad:** agregar o eliminar una foto registra un evento `photo_added` /
  `photo_deleted` que aparece en la **cadena de custodia** del producto («Foto
  agregada · quién · tipo · nota»).
- **Tamaño del repositorio:** ~60–100 KB por foto implican unas 10–15 mil fotos por
  GB; GitHub recomienda repositorios por debajo de 1 GB (límite duro de 100 GB y
  100 MB por archivo). Conviene 1–3 fotos por objeto, no una por movimiento. Eliminar
  una foto (maestro) la borra del árbol, pero el historial de git conserva el binario:
  para recuperar espacio hay que reescribir el historial del repositorio de datos.
  Eliminar un ítem quita sus fotos del índice y deja los JPEG en el repositorio.

### Chips NFC (Chips NFC)

Cada producto o ubicación puede llevar un chip NFC (etiquetas NTAG; se recomienda **NTAG215**,
y *anti-metal* sobre estantes metálicos). El chip guarda **una URL** como
`https://<tu-app>/escanear?nfc=2-1-01-01-001&s=K7Q2MX9AB0`: al acercar el teléfono se abre la app
en el navegador, sin instalar nada (Android; iPhone XS o posterior; no se usa Web NFC).

- **Prueba de presencia.** Cada toque guarda un evento `nfc_tap` (persona, código y hora) en
  `trace_events`. Si la persona no ha iniciado sesión, el toque espera y se registra al entrar;
  `?nfc=` se borra de la URL tras procesarlo y el mismo chip tocado dos veces en un minuto cuenta
  una vez. La app muestra el producto como en Escanear.
- **Ruta verificable.** Un toque confirma el punto de control de su etiqueta (método
  «Chip NFC tocado») igual que escanear el código de barras, y completa el comprobante habitual.
- **Firma.** Con `NFC_SECRET` en los Secrets, cada URL lleva `&s=<firma>` (HMAC-SHA256 truncado a
  10 caracteres) y la app rechaza URL sin firma, alteradas o inventadas. Configúralo **antes** de
  grabar los chips: si lo cambias, hay que regrabarlos. Sin `NFC_SECRET` las URL van sin firma.
  `APP_URL` fija la dirección pública que se graba (por omisión, la dirección con la que se abrió
  la app).
- **Inventario con el teléfono** (profesor/maestro, página *Chips NFC*). Se abre un conteo (todo,
  una estantería o un contenedor); cada chip tocado, o código escrito/escaneado, marca el producto
  como verificado, con la cantidad contada opcional. Se ve el avance, lo que falta y las
  diferencias respecto a lo que debería haber en el estante (total − en préstamo). Al cerrar se
  guarda un resumen (`count_closed`). **Contar no cambia cantidades**: solo el maestro puede
  aplicar ajustes, uno por uno y con confirmación explícita, y quedan en el historial del producto.
- **Grabar un chip con NFC Tools** (gratuita, Android e iPhone): en *Chips NFC → Grabar etiquetas*
  elige el producto y copia su URL; en NFC Tools ve a **Escribir → Agregar un registro →
  URL/URI**, pega la URL, toca **Escribir** y acerca el chip. Prueba el chip acercando de nuevo el
  teléfono (debe abrir la app y aparecer como «Probado»). Bloquear el chip es opcional y
  permanente: hazlo solo tras probarlo.
- El registro de chips (grabado, probado, retirado) y los conteos viven en la hoja `trace_events`
  (eventos `nfc_tap`, `nfc_tag_written`, `nfc_tag_removed`, `count_started`, `count_mark`,
  `count_closed`); no hay hojas nuevas.

### Generar productos desde la descripción

En **📦 Inventario**, cada Contenedor Principal con descripción ofrece
**✨ Generar productos desde la descripción**: interpreta el texto (por ejemplo,
«Contiene Piezas Lego de Pines 4x2 - 2x2 - 2x1») y propone un Contenedor de
Característica por tipo de pieza o caja interna, con código consecutivo dentro del
contenedor (2-1-01-01-000, 2-1-01-02-000…) y la ubicación heredada. La vista previa
es editable (crear sí/no, nombre, código, cantidad, unidad), señala lo ambiguo
(como una medida «1x0») y crea todo con **una sola escritura** en la base. Las
cantidades no vienen en la descripción: escríbelas antes de crear o quedan como
«pendiente de conteo». El catálogo se muestra agrupado por contenedor.

### Salud del inventario (📊 Reportes)

La pestaña **🩺 Salud del inventario** audita los datos: códigos y jerarquía,
ubicación textual frente al código, errores de digitación («Contendor»),
categorías escritas de varias formas, medidas imposibles, cantidades pendientes y
contenido descrito sin productos. Las correcciones seguras (como normalizar la
ubicación desde el código) se revisan en una tabla antes/después y solo se aplican
al confirmar; quedan en el historial del ítem.

### Eliminar ítems

En **Inventario → ✏️ Editar → 🗑️ Eliminar definitivamente** el ítem se borra
**para siempre**: desaparecen su fila, su historial y sus préstamos ya devueltos, y
el código queda libre para volver a registrarse (antes "eliminar" solo lo marcaba
como dado de baja y el código seguía bloqueado: *"ya existe un item con ese
código"*). Al eliminar un Contenedor Principal también se eliminan los ítems que
contiene. Se pide confirmación y se muestra qué se perderá; se bloquea si hay
préstamos abiertos (hay que registrar antes el reingreso) y las solicitudes de
producto pendientes o aprobadas se cancelan. Los códigos de ítems que ya estaban
dados de baja también se pueden registrar de nuevo. Cada cambio se guarda y se
publica en el momento (ver *Integridad de datos*).

### Fechas de devolución

La fecha límite de un préstamo vence al **terminar ese día en Colombia**
(`America/Bogota`), no a las 7 p. m. del día anterior. Una fecha con hora concreta
se respeta tal cual.

### Corrección de usuarios

El maestro puede corregir nombre, ID, correo institucional y programa. El
sistema valida campos obligatorios, dominio institucional y unicidad del correo;
rol, estado y contraseña conservan sus controles independientes.

### Restablecer contraseñas

Cuando un estudiante o profesor olvida su contraseña, el maestro la restablece en
**Usuarios → tarjeta del usuario → 🔑 Restablecer contraseña**:

1. Elige **Generar una contraseña temporal** (12 caracteres aleatorios en tres
   grupos, p. ej. `hX7k-m3Pq-9tRw`, sin caracteres que se confunden como `0/O` o
   `1/l/I`) o **Escribirla yo** (con confirmación; mínimo 8 caracteres, la misma
   regla del registro).
2. Confirma con **Sí, restablecer**. La contraseña anterior deja de funcionar y las
   sesiones abiertas de esa cuenta se cierran en su siguiente recarga.
3. La contraseña generada se muestra **una sola vez** (con botón de copiar):
   entrégala en persona. Si hay SMTP configurado, una casilla opcional
   (desmarcada por defecto) la envía al correo institucional del usuario.

Al ingresar con ella, el usuario ve una pantalla que le pide elegir una
contraseña propia antes de usar la app (no puede repetir la temporal); mientras
tanto su tarjeta muestra la pastilla **Clave temporal**. Si estaba bloqueado por
intentos fallidos, el restablecimiento lo desbloquea. Las demás cuentas inician
sesión como siempre.

**Política:** solo un maestro activo puede restablecer, y solo a estudiantes y
profesores. Cada maestro cambia su propia contraseña en **Mi perfil** (pide la
actual) y la de otro maestro no se restablece desde aquí, para que nadie tome en
silencio la cuenta de otro maestro; si un maestro pierde el acceso, otro maestro
puede pasarlo a profesor, restablecerla y devolverle el rol (un paso deliberado y
visible). Cada restablecimiento queda auditado en la hoja `trace_events` (evento
`password_reset`: quién, a quién, cuándo, modo y si se envió correo) **sin la
contraseña**. La hoja `users` suma las columnas `must_change_password` y
`password_changed_at`; en una base anterior se completan solas al cargar.

## Roles

| Rol | Puede |
|---|---|
| Estudiante | Escanear, solicitar productos/servicios, reservar y gestionar sus préstamos/solicitudes |
| Profesor | Todo lo anterior + alta/edición/baja de ítems, agregar fotos de los objetos, aprobar solicitudes/reservas, ver préstamos y reportes |
| Maestro | Todo lo anterior + eliminar fotos, corregir usuarios, roles/estados, restablecer contraseñas, lista blanca y exportación |

**Seguridad del registro:** nadie elige su rol al registrarse. Toda cuenta nace
`estudiante`; solo nace `profesor` si su correo está en la lista blanca
(gestionada por un `maestro`) **y** el solicitante demuestra ser dueño del correo
con un código de 6 dígitos enviado a esa dirección (vence en 15 minutos, 5
intentos). Sin esa prueba cualquiera podría registrarse con el correo de un
profesor. Si el correo no puede verificarse (SMTP sin configurar) la cuenta nace
`estudiante` y el `maestro` puede activar el rol en **Usuarios**. El rol `maestro` nunca se auto-asigna: la
primera cuenta maestra se siembra desde los Secrets de Streamlit
(`MASTER_EMAIL` / `MASTER_INITIAL_PASSWORD`) la primera vez que arranca la app.
Se acepta cualquier correo institucional (dominio terminado en `.edu` o
`.edu.co`), no está restringido a una sola institución. El formulario de
registro exige, sin excepción: nombre completo (nombre y apellido), **ID
Estudiante**, correo institucional, programa académico o departamento, y
contraseña — todos obligatorios.

**Inicio de sesión y sesión:**

- El error es el mismo si la cuenta no existe o la contraseña es incorrecta (no
  revela qué correos están registrados) y hay bloqueo temporal tras 5 intentos
  fallidos por correo (`LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_MINUTES`).
- La sesión se **revalida contra la base en cada recarga**: un cambio de rol o una
  cuenta deshabilitada surten efecto de inmediato, y la sesión expira a las 12 h
  (`SESSION_TIMEOUT_MINUTES`). La sesión nunca guarda el hash de la contraseña.
- Si el maestro restablece la contraseña de una cuenta, las sesiones de esa cuenta
  abiertas antes del cambio se cierran (`password_changed_at`), y al volver a entrar
  debe elegir una contraseña propia (ver *Restablecer contraseñas*).
- Las vistas de Inventario, Reportes y Usuarios vuelven a comprobar el rol (ocultar
  una página no es autorizar) y el texto escrito por usuarios se escapa antes de
  mostrarse como HTML.

## Arquitectura

- **Frontend/backend**: Streamlit, con navegación agrupada por secciones
  (`st.navigation`): Principal, Gestión, Administración y Mi cuenta — visibles
  según el rol de quien inició sesión. La interfaz usa un sistema de diseño
  propio (`style.css` + componentes de `core/ui.py`) con la paleta institucional
  de UNIMINUTO (azul Pantone 287 C `#003698`, amarillo Pantone 116 C `#FFCE00`),
  íconos monocromáticos Material Symbols, composición simétrica (encabezados,
  indicadores y accesos centrados; filas incompletas centradas), modo oscuro y
  una vista de celular tipo app (símbolo centrado en la cabecera, mosaicos de
  dos columnas).
- **Base de datos**: un archivo Excel (`UNIMINUTO_LAB_DB.xlsx`) que vive en el
  repositorio **privado** `GIUSEPPESAN21/UNIMINUTO-Lab-Database`. Cada
  escritura se guarda localmente y se sincroniza a GitHub vía API en un hilo
  síncrono y serializado (ver *Integridad de datos*).
- **Autenticación**: contraseñas con `bcrypt`, sesión con `st.session_state`
  revalidada en cada recarga.

```
app.py                  Punto de entrada: config, CSS, sesión, navegación agrupada por rol
core/
  config.py              Acceso seguro a st.secrets (nunca lanza si faltan)
  permissions.py         Roles y comprobaciones de permiso centralizados
  storage.py             Capa de datos: Excel local (escritura atómica) + sync a GitHub
  auth.py                 Registro, login, reglas de rol
  labels.py                Nomenclatura de tipos de item (UI) y etiquetas en formato clásico a su tamaño real (PNG/PDF/prueba)
  ui.py                    Componentes visuales compartidos (encabezados, tarjetas, indicadores, línea de tiempo)
  traceability.py         Ruta verificable, comprobantes, líneas de tiempo y cadena de custodia
  photos.py               Fotos de los objetos: limpieza de la imagen, GitHub/disco, índice y caché

  nfc.py                   Chips NFC: URL y firma de cada chip, toques, ruta por NFC y registro de chips
  inventory_count.py      Inventario con el teléfono: conteos, avance, diferencias y ajustes del maestro
  inventory_suggestions.py Productos propuestos a partir de la descripción de un contenedor
  data_quality.py         Auditoría de calidad del inventario y correcciones seguras
  barcode.py              Validación/lectura de codigos (GLIOPS V3) y resolución de escaneo
  loans.py                Checkout / checkin / vencidos
  reservations.py         Validación, conflictos y aprobación de reservas
  service_requests.py     Solicitudes de productos/servicios y revisión
  location.py             Rutas de ubicación derivadas del código GLIOPS (con etiquetas de estantería, piso y mesa)
  places.py               Ubicaciones con código: altas en bloque, árbol y contenido de cada una
  notifications.py        Correo SMTP + alertas WhatsApp opcionales
  email_events.py         Correos automáticos por evento (destinatarios, contenido, envío en segundo plano, registro)
  reports.py              Analítica y exportación a Excel
views/
  login.py, inicio.py, escanear.py, inventario.py, solicitudes.py, trazabilidad.py,
  reservas.py, location_guide.py, label_settings.py, photo_panel.py, perfil.py, prestamos.py, usuarios.py,
  reportes.py, correos.py (pestaña de Reportes), ubicaciones.py, acerca_de.py, nfc.py (página Chips NFC),
  nfc_tap.py (toque con ?nfc=)
tests/                  Pruebas unitarias de core/* (pytest, sin tocar Excel/GitHub)
.github/workflows/ci.yml Integración continua: sintaxis + pruebas en cada push/PR
```

## Integridad de datos

Cómo se protege la base (Excel + GitHub):

- **Escritura atómica:** el Excel se escribe a un archivo temporal y se reemplaza de
  una vez (`os.replace`): nunca queda un archivo a medias, ni siquiera si el proceso
  se interrumpe.
- **Guardado en el momento:** cada operación (alta, edición, eliminación, préstamo…)
  se publica en GitHub **antes de terminar**, de forma síncrona y serializada. Varias
  personas escribiendo a la vez no se pisan: se publica siempre la última versión.
  Ante un fallo transitorio de red o un 5xx se reintenta; si aun así falla, el
  cambio queda guardado localmente, se avisa con un banner y hay un botón
  **🔄 Reintentar sincronización**.
- **Nunca se pisa lo que la app no conoce:** la app recuerda la versión (SHA) que
  descargó/subió por última vez. Si GitHub tiene otra (alguien editó el archivo, hay
  otra instancia escribiendo, o no se pudo descargar al arrancar) el push se
  **rechaza** y aparece un *conflicto*: el perfil maestro elige entre
  **⬇️ conservar la versión de GitHub** o **⬆️ conservar la de la app**. La versión
  que se reemplace sigue en el historial de commits del repositorio de datos.
  Esto evita, por ejemplo, que un arranque sin red publique una base vacía sobre la
  real.
- **Stock y reservas bajo concurrencia:** la disponibilidad y los cruces de horario
  se **re-verifican dentro del lock** de la base; dos personas no pueden sacar la
  misma última unidad ni aprobar dos reservas que se cruzan.
- **Recarga fiel:** al releer el Excel las celdas vacías son cadenas vacías (no
  `NaN`) y textos como `NA` o `None` se conservan.

> **Importante al limpiar o restaurar la base a mano:** si cambias
> `UNIMINUTO_LAB_DB.xlsx` en el repositorio de datos mientras la app está corriendo,
> la app lo detectará como conflicto en su siguiente escritura. Lo más simple es
> reiniciar la app (Reboot) después de cambiar el archivo, para que descargue la
> versión nueva.

## Importación masiva de inventario

En **Inventario → Importar CSV masivo** puedes subir un CSV con columnas
`id,name,category,description,item_type,parent_id,unit,quantity,location,min_stock_alert`
para cargar de una sola vez el catálogo inicial del laboratorio (útil para tu
serie extensa de códigos de barras ya impresos). El botón de la pestaña
descarga una plantilla de ejemplo. Toda la importación se sincroniza a
GitHub en un solo commit, no uno por fila. Si vas a importar Contenedores
Principales junto con sus Contenedores de Característica, coloca la fila del
Contenedor Principal (`item_type=master`) **antes** que la de sus
Contenedores de Característica (`item_type=child`) en el CSV.

## Pruebas automatizadas

```bash
pip install -r requirements-dev.txt
ruff check .
python -m pytest -q --cov=core --cov=views --cov=app --cov-report=term-missing:skip-covered
```

Las pruebas son **herméticas**: un fixture automático (`tests/conftest.py`) envía el
Excel a una carpeta temporal, desactiva GitHub/SMTP aunque exista un
`.streamlit/secrets.toml` real y reinicia la caché, de modo que ninguna prueba
escribe en el repositorio ni en la base real, y se pueden repetir sin límite. Hay un
`FakeStorage` en memoria que cumple el mismo contrato público que `LabStorage`, un
GitHub simulado (SHA, 404, 409) para probar la sincronización y pruebas de extremo a
extremo con `AppTest` (sesión, registro con verificación, permisos, eliminación). El
CI ejecuta lint, sintaxis y pruebas con cobertura mínima del 60 % en Python 3.11 y
3.12 en cada push/PR; Dependabot propone las actualizaciones de dependencias.

## Configuración (Secrets de Streamlit)

Copia `.streamlit/secrets.toml.example`, complétalo con tus valores reales y
pégalo en Streamlit Cloud → tu app → Settings → Secrets (o guárdalo como
`.streamlit/secrets.toml` en local; ese archivo está en `.gitignore` y nunca
debe subirse al repositorio).

El `GITHUB_TOKEN` debe ser un *fine-grained personal access token* con acceso
**únicamente** al repositorio `UNIMINUTO-Lab-Database` y permiso
"Contents: Read and write". No reutilices tokens con acceso a otros
repositorios. Las fotos de los objetos usan este mismo token y repositorio
(carpeta `fotos/`, ver *Fotos de los objetos*).

Para correo automático configura `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `SMTP_FROM_EMAIL` y TLS/SSL en Secrets (ver *Correos
automáticos*). Nunca subas credenciales al repositorio. SMTP es opcional:
su ausencia no impide guardar una solicitud o reserva (pero sin SMTP no se puede
verificar el correo de un profesor: ver *Seguridad del registro*).

### Correos automáticos

Todo lo que se pide en el software avisa por correo, en un hilo en segundo plano
(la interfaz no espera al servidor y un fallo nunca invalida la operación):

| Evento | Quién recibe |
|---|---|
| Nueva solicitud de producto o servicio | Perfiles maestro |
| Nueva reserva (actividad o laboratorio completo) | Perfiles maestro |
| Salida (checkout) y devolución (checkin) de un préstamo | Perfiles maestro |
| Solicitud o reserva aprobada / rechazada | Quien la pidió |
| Resumen de préstamos vencidos (botón manual) | Perfiles maestro |

Asunto de ejemplo: `[Laboratorio] Nueva solicitud: Microscopio — Ana Prueba
(Estudiante)`. El cuerpo (texto plano + HTML) dice quién (nombre, rol, correo, ID),
qué (producto y código, cantidad, servicio, fechas) y cuándo (hora de Bogotá).

**Secrets** (Streamlit Cloud → Settings → Secrets). Ejemplo con Gmail y
contraseña de aplicación (activa antes la verificación en 2 pasos de la cuenta):

```toml
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USE_TLS = true
SMTP_USE_SSL = false            # true solo con el puerto 465
SMTP_USERNAME = "laboratorio@gmail.com"
SMTP_PASSWORD = "abcd efgh ijkl mnop"   # contraseña de aplicación, no la clave normal
SMTP_FROM_EMAIL = "laboratorio@gmail.com"

APP_URL = "https://tu-app.streamlit.app"   # opcional: enlace dentro de cada correo
ADMIN_NOTIFICATION_EMAILS = ["coordinacion@uniminuto.edu.co"]  # opcional: destinatarios extra
```

Destinatarios = perfiles maestro **activos** + `ADMIN_NOTIFICATION_EMAILS`
(+ profesores activos si el maestro lo activa), sin duplicados y sin correos
inválidos o anónimos. En **Reportes → Correos** (solo maestro) se ve si el SMTP está
configurado (nunca se muestran los valores), se activa o apaga cada tipo de aviso
(todos activos por defecto, guardado en la hoja `settings`), se envía un **correo de
prueba** y se consultan los últimos envíos y sus errores (hoja `notification_log`,
últimos 500). Sin SMTP configurado no se envía nada y la app funciona igual.
La lógica vive en `core/email_events.py`.

Para los chips NFC: `APP_URL` (dirección pública de la app, p. ej.
`https://mi-laboratorio.streamlit.app`) y `NFC_SECRET` (secreto para firmar las URL de los
chips; opcional pero recomendado). Ver *Chips NFC*.

Opcionales de seguridad (valores por defecto entre paréntesis):
`LOGIN_MAX_ATTEMPTS` (5), `LOGIN_LOCKOUT_MINUTES` (5) y `SESSION_TIMEOUT_MINUTES` (720).

## Ejecutar en local

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Licencia

MIT. Ver [LICENSE](LICENSE).
