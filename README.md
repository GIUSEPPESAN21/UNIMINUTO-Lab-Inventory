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

Desde Inventario o Escanear, el menú **🏷️ Etiqueta** ofrece dos formatos:

- **PDF · imprimir (recomendado):** página física exacta de **50×25 mm**, sin márgenes internos del documento. El raster monocromático de 384×192 px se centra manteniendo un punto de imagen por punto de la SAT TT 460 a 203 dpi; así el navegador o visor no lo reduce como ocurrió al imprimir el PNG anterior.
- **PNG · respaldo:** imagen original de **384×192 px a 203 dpi** para archivo, integración o control manual del driver.

La etiqueta incluye logotipo oficial de UNIMINUTO, Laboratorio de Ingeniería, tipo de activo, aviso institucional, nombre en una banda negra de alto contraste, categoría, ubicación, Code 128 y código legible. Los títulos usan espaciado independiente entre letras y palabras. Además, los códigos estructurados muestran una ruta física compacta —por ejemplo, `RUTA: E2 › P1 › C01 › CJ02 › I003`— donde `E`, `P`, `C`, `CJ` e `I` significan estantería, piso, contenedor, caja e ítem. Los niveles `00/000` que no aplican se omiten de la ruta, pero el valor codificado permanece intacto. Los códigos Mesa y Lego generan su guía equivalente; los códigos libres conservan la ubicación textual. Los datos opcionales vacíos se omiten sin bloquear la impresión.

Para imprimir el PDF en la SAT TT 460 selecciona **papel 50×25 mm**, orientación **horizontal**, escala **100 % / tamaño real** y **sin márgenes**. No uses “Ajustar”, “Encoger” ni “Varias páginas por hoja”. Las barras conservan zonas de silencio, ancho entero por módulo y altura mínima de 9 mm.
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

### Guía móvil de ubicación

Cada producto muestra pasos interactivos derivados del código V3 (estantería,
piso, contenedor, caja e ítem). Mesa y Lego tienen rutas especiales; códigos
libres/heredados usan `location`. No se dibuja un mapa porque los documentos no
incluyen plano, coordenadas ni punto de entrada.

### Corrección de usuarios

El maestro puede corregir nombre, ID, correo institucional y programa. El
sistema valida campos obligatorios, dominio institucional y unicidad del correo;
rol, estado y contraseña conservan sus controles independientes.
## Roles

| Rol | Puede |
|---|---|
| Estudiante | Escanear, solicitar productos/servicios, reservar y gestionar sus préstamos/solicitudes |
| Profesor | Todo lo anterior + alta/edición/baja de ítems, aprobar solicitudes/reservas, ver préstamos y reportes |
| Maestro | Todo lo anterior + corregir usuarios, roles/estados, lista blanca y exportación |

**Seguridad del registro:** nadie elige su rol al registrarse. Toda cuenta nace
`estudiante`; solo nace `profesor` si su correo ya está en la lista blanca
(gestionada por un `maestro`). El rol `maestro` nunca se auto-asigna: la
primera cuenta maestra se siembra desde los Secrets de Streamlit
(`MASTER_EMAIL` / `MASTER_INITIAL_PASSWORD`) la primera vez que arranca la app.
Se acepta cualquier correo institucional (dominio terminado en `.edu` o
`.edu.co`), no está restringido a una sola institución. El formulario de
registro exige, sin excepción: nombre completo (nombre y apellido), **ID
Estudiante**, correo institucional, programa académico o departamento, y
contraseña — todos obligatorios.

## Arquitectura

- **Frontend/backend**: Streamlit, con navegación moderna agrupada por
  secciones (`st.navigation`): Principal, Gestión del laboratorio,
  Administración y Mi cuenta — visibles según el rol de quien inició sesión.
- **Base de datos**: un archivo Excel (`UNIMINUTO_LAB_DB.xlsx`) que vive en el
  repositorio **privado** `GIUSEPPESAN21/UNIMINUTO-Lab-Database`. Cada
  escritura se guarda localmente y se sincroniza a GitHub vía API en un hilo
  de fondo.
- **Autenticación**: contraseñas con `bcrypt`, sesión con `st.session_state`.

```
app.py                  Punto de entrada: config, CSS, sesión, navegación agrupada por rol
core/
  config.py              Acceso seguro a st.secrets (nunca lanza si faltan)
  storage.py             Capa de datos: Excel local + sync a GitHub
  auth.py                 Registro, login, reglas de rol
  labels.py                Nomenclatura de tipos de item (UI) y generación de etiquetas imprimibles
  ui.py                    Componentes visuales compartidos (logo, encabezados)
  barcode.py              Validación/lectura de codigos (GLIOPS V3) y resolución de escaneo
  loans.py                Checkout / checkin / vencidos
  reservations.py         Validación, conflictos y aprobación de reservas
  service_requests.py     Solicitudes de productos/servicios y revisión
  location.py             Rutas de ubicación derivadas del código GLIOPS
  notifications.py        Correo SMTP + alertas WhatsApp opcionales
  reports.py              Analítica y exportación a Excel
views/
  login.py, inicio.py, escanear.py, inventario.py, solicitudes.py,
  reservas.py, location_guide.py, perfil.py, prestamos.py, usuarios.py,
  reportes.py, acerca_de.py
tests/                  Pruebas unitarias de core/* (pytest, sin tocar Excel/GitHub)
.github/workflows/ci.yml Integración continua: sintaxis + pruebas en cada push/PR
```

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
python -m pytest -q
```

Las pruebas usan un `FakeStorage` en memoria (`tests/conftest.py`) que
cumple el mismo contrato público que `LabStorage`, por lo que corren en
segundos y no requieren Excel, GitHub ni Secrets configurados. Se ejecutan
automáticamente en cada push/PR vía GitHub Actions.

## Configuración (Secrets de Streamlit)

Copia `.streamlit/secrets.toml.example`, complétalo con tus valores reales y
pégalo en Streamlit Cloud → tu app → Settings → Secrets (o guárdalo como
`.streamlit/secrets.toml` en local; ese archivo está en `.gitignore` y nunca
debe subirse al repositorio).

El `GITHUB_TOKEN` debe ser un *fine-grained personal access token* con acceso
**únicamente** al repositorio `UNIMINUTO-Lab-Database` y permiso
"Contents: Read and write". No reutilices tokens con acceso a otros
repositorios.

Para correo automático configura `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `SMTP_FROM_EMAIL` y TLS/SSL en Secrets. Define
`ADMIN_NOTIFICATION_EMAILS` como lista; si queda vacía, se usan profesores y
maestros activos. Nunca subas credenciales al repositorio. SMTP es opcional:
su ausencia no impide guardar una solicitud o reserva.

## Ejecutar en local

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Licencia

MIT. Ver [LICENSE](LICENSE).
