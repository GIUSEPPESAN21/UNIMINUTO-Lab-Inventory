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
debe cumplir uno de estos 3 formatos — validados por regex en
`core/barcode.py`. Los items ya cargados con una nomenclatura anterior
siguen funcionando con normalidad (solo se valida al crear, no al editar).

| Formato | Patrón | Ejemplo | Uso |
|---|---|---|---|
| Estándar (5 niveles) | `[ESTANTERIA 1-3]-[PISO 1-6]-[CONTENEDOR]-[CAJA]-[ITEM]` | `1-2-05-12-001` | Inventario masivo: insumos, piezas, electrónica |
| Mesas de trabajo | `M1-E[n]` o `M2-E[n]` | `M1-E2` | Equipos de alto valor (impresora 3D, cortadora láser...) |
| Exhibición Lego | `E3-LM[n]` | `E3-LM07` | Modelos armados en la Estantería 3 |

### Etiquetas imprimibles

Desde el catálogo de Inventario o desde Escanear, el botón
**🏷️ Descargar etiqueta** genera un PNG con el código de barras (Code128)
centrado y el código en texto legible debajo, en un lienzo de 384×192px
(≈ 50×25mm a 203dpi) listo para una impresora térmica **SAT TT 460**.

## Roles

| Rol | Puede |
|---|---|
| Estudiante | Ver catálogo, escanear y pedir salida a su nombre, ver y reingresar sus propios préstamos |
| Profesor | Todo lo anterior + alta/edición/baja de ítems, registrar salida/reingreso de cualquier usuario, ver todos los préstamos y reportes |
| Maestro | Todo lo anterior + gestión de usuarios y roles, lista blanca de profesores, auditoría completa, exportar base de datos |

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
  notifications.py        Alertas WhatsApp opcionales (Twilio)
  reports.py              Analítica y exportación a Excel
views/
  login.py, inicio.py, escanear.py, inventario.py, perfil.py,
  prestamos.py, usuarios.py, reportes.py, acerca_de.py
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

## Ejecutar en local

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Licencia

MIT. Ver [LICENSE](LICENSE).
