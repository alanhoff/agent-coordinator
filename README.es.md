<h1 align="center">Agent Coordinator</h1>
<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.pt-BR.md">Português (Brasil)</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>
<p align="center"><strong>Dale a Codex una tarea compleja. Obtén un plan claro y un resultado verificado.</strong></p>
<p align="center">Mantén los trabajos largos comprensibles, facilita el seguimiento del progreso y, tras una interrupción, retómalos hasta llegar a un resultado verificado.</p>
<p align="center">
  <img src=".github/readme/agent-coordinator-hero.png" width="880" alt="Ilustración de una solicitud compleja que avanza por varias rutas de trabajo delimitadas, con puntos de control, y regresa como un único resultado verificado.">
</p>
<p align="center">
  <a href="#instala-con-un-solo-prompt"><strong>Instálalo con un solo prompt</strong></a>
  ·
  <a href="#ejemplo-con-una-tarea-cotidiana">Ver un ejemplo cotidiano</a>
</p>
<p align="center"><sub>Licencia MIT · Se instala en tu cuenta de usuario · No cambia la configuración de Codex</sub></p>

## Lo que obtienes

- Un plan claro que puedes seguir desde la solicitud hasta el final.
- Partes con responsabilidades claras, cada una con un propósito y un responsable definidos.
- Un resultado verificado y recuperable que resiste las interrupciones.

## ¿Agent Coordinator es adecuado para ti?

| Úsalo cuando | Probablemente no lo necesitas cuando |
|---|---|
| El trabajo incluye varios pasos, archivos o especialidades interdependientes. | La tarea consiste en un único paso pequeño y evidente. |
| Varias partes independientes pueden avanzar de forma segura. | Basta con una respuesta rápida o una edición mínima. |
| Una interrupción dificultaría reconstruir el progreso. | Podrías empezar de nuevo fácilmente a partir del prompt original. |

## Ejemplo con una tarea cotidiana

> Añade búsquedas guardadas a mi aplicación sin romper el proceso de pago.

1. **Define con claridad el resultado esperado:** identifica el comportamiento de las búsquedas guardadas, la medida de protección para el proceso de pago y cómo se verificará cada elemento.
2. **Mantén el progreso comprensible:** separa la exploración, el cambio acotado y la prueba de regresión para que cada parte tenga un propósito claro.
3. **Verifica antes de terminar:** revisa los archivos modificados y la evidencia de verificación; tras una interrupción, continúa desde el progreso registrado en lugar de empezar de nuevo.

La tarea solo termina cuando la búsqueda guardada cumple su criterio de aceptación y las comprobaciones existentes del proceso de pago siguen dando resultados satisfactorios.

## Instala con un solo prompt

Pide a Codex que siga el [instalador incluido en el repositorio](INSTALL.md):

```text
Instala https://github.com/alanhoff/agent-coordinator siguiendo INSTALL.md
```

El procedimiento clona el repositorio en un directorio temporal, registra su commit, ensambla una skill nueva, la instala para el usuario actual, elimina la copia temporal e informa de la ruta de instalación y del commit de origen. Solo reemplaza un destino existente si este se identifica como Coordinator.

Para ejecutar Coordinator se necesita Python 3.11 o posterior y no se requieren paquetes de terceros en tiempo de ejecución. La instalación no modifica la configuración de Codex ni registra perfiles globales de agentes personalizados.

| Ubicación para el usuario actual | Contenido |
|---|---|
| `~/.agents/skills/coordinator` | La skill, los perfiles de roles, las referencias, los adaptadores de Python y el código del entorno de ejecución incluido |
| `~/.agent-coordinator` | Sesiones privadas, bloqueos, datos de recuperación y estado del flujo de trabajo |

## Prueba una primera tarea

En un proyecto, envía este prompt inicial:

```text
$coordinator Revisa el README de este proyecto para detectar pasos de configuración confusos. No edites
archivos. Devuelve las tres correcciones de mayor impacto, cita la evidencia de cada una y confirma
que no cambió ningún archivo.
```

Una respuesta satisfactoria cumple tres condiciones:

1. Las tres correcciones están ordenadas por impacto.
2. Cada corrección cita evidencia del proyecto.
3. La respuesta confirma que no cambió ningún archivo.

## Cómo funciona

Coordinator sigue los mismos cuatro pasos en cada trabajo:

1. **Comprender:** define de forma explícita el resultado solicitado, las restricciones y la prueba de éxito.
2. **Dividir:** divide el trabajo en las partes útiles más pequeñas, con límites y dependencias claros.
3. **Hacer:** ejecuta en un orden seguro las partes que estén listas. Los agentes especialistas son opcionales; si no están disponibles, Coordinator realiza cada parte directamente mediante el mismo proceso.
4. **Verificar y recuperar:** inspecciona el resultado y su evidencia, comprueba el estado del trabajo incierto antes de reintentarlo y solo termina cuando se cumplen los requisitos y se resuelven los bloqueos.

La secuencia persistente de comandos convierte esos pasos en operaciones seguras:

```text
plan-apply → next → node-route-auto → node-claim → node-start → node-complete
           ↘ refine/split/reconcile según sea necesario ↗
                         workflow-complete
```

`next` es de solo lectura e informa la siguiente clase de acción permitida sin incluir el estado completo del flujo de trabajo.

## Preguntas frecuentes

<details>
<summary>¿Requiere varios agentes?</summary>

No. Cuando hay capacidad disponible para agentes adicionales, Coordinator puede encargar partes independientes a agentes distintos; de lo contrario, las completa directamente, una por una.

</details>

<details>
<summary>¿Tengo que invocarlo de forma explícita?</summary>

Sí. El patrón de prompt documentado inicia cada tarea coordinada con `$coordinator`; la instalación se limita a instalar la skill y no cambia ninguna configuración.

</details>

<details>
<summary>¿Qué añade a mi proyecto o configuración?</summary>

No añade al proyecto de destino ningún archivo persistente administrado por Coordinator ni modifica la configuración de Codex o los perfiles globales de agentes personalizados. Durante la inicialización, crea y elimina un archivo con nombre privado únicamente para detectar si el sistema de archivos del repositorio distingue entre mayúsculas y minúsculas; Windows lo determina sin usar un archivo de prueba.

</details>

<details>
<summary>¿Qué ocurre si se interrumpe el trabajo?</summary>

Una nueva ejecución de Coordinator puede reanudarse a partir del estado privado. Marca el trabajo que aún podría estar activo para comprobar su estado antes de volver a intentarlo, conserva la evidencia de las partes completadas y evita iniciar dos veces un paso cuyo estado no está claro.

</details>

## Referencia

<details>
<summary>Patrones de prompt</summary>

Usa un prefijo `$coordinator` explícito e indica el resultado esperado. Estos patrones cubren la implementación, el diagnóstico, la recuperación y la comparación basada únicamente en evidencia.

```text
$coordinator Implementa la función de búsquedas guardadas. Revisa las instrucciones del repositorio,
conserva los criterios de aceptación, separa solo el trabajo independiente, valida el comportamiento
integrado y termina con evidencia concreta.
```

```text
$coordinator Reproduce y diagnostica la prueba intermitente del proceso de pago antes de modificar el código de producción. Organiza como partes dependientes el diagnóstico, la corrección mínima en la
capa responsable y la validación independiente.
```

```text
$coordinator Reanuda el flujo de trabajo interrumpido de migración del esquema. Comprueba el estado del trabajo incierto antes de reintentarlo,
conserva la evidencia de las partes completadas y valida el comportamiento de la
reversión y de la migración hacia delante.
```

```text
$coordinator Compara las arquitecturas propuestas para el procesamiento de eventos con los límites y
requisitos actuales del repositorio. Expón las ventajas, las desventajas y la evidencia que falta;
después, recomienda una sin implementar ninguna de las dos opciones.
```

</details>

<details>
<summary>Evaluación y ciclo de vida</summary>

Coordinator registra cinco dimensiones de complejidad de 0 a 4: alcance, superficie de cambio, acoplamiento, novedad y verificación. Por separado, registra factores de ambigüedad de 0 a 4 para el objetivo, las entradas, los límites, las dependencias y la aceptación.

Los límites predeterminados son inclusivos: un total de complejidad de 6 o más, o cualquier dimensión con un valor de 3 o más, exige dividir; un total de ambigüedad de 4 o más, o cualquier factor con un valor de 2 o más, exige refinar. La profundidad máxima predeterminada de refinamiento es 8.

```text
assess → refine or split → route → claim → execute → validate → reassess
```

Antes de iniciar el enrutamiento, toda hoja evaluable que no esté bloqueada debe estar vigente y ser ejecutable. Los cambios en los requisitos o en los resultados vigentes de las dependencias pueden dejar desactualizado el trabajo posterior, por lo que la comprobación de punto fijo se repite después de cambios pertinentes en la evidencia.

</details>

<details>
<summary>Responsabilidad, enrutamiento y finalización</summary>

- Cada parte ejecutable tiene criterios de aceptación, un rol y cero o más `write_scopes` normalizados relativos al repositorio.
- La ausencia de ámbitos —es decir, una lista `write_scopes` vacía— indica trabajo basado únicamente en evidencia y requiere `change_surface=0`. El trabajo sobre artefactos requiere una puntuación positiva de superficie de cambio y al menos un ámbito.
- No puede haber solapamiento de ámbitos entre trabajos activos independientes. La comparación entre mayúsculas y minúsculas sigue el comportamiento detectado en el sistema de archivos de destino.
- El enrutamiento solo clasifica los candidatos anunciados por el entorno de ejecución activo. Si no hay un catálogo vigente disponible o la selección falla, la ejecución hereda el modelo y el nivel de esfuerzo del elemento padre.
- Al reclamar el trabajo, se registra una línea base SHA-256 para cada ámbito de artefactos. Para completar una parte, cada ámbito declarado debe seguir materializado y haber cambiado durante ese intento.
- Para completar el flujo de trabajo, deben cumplirse los requisitos y resolverse los bloqueos, la evidencia debe ser válida y solo puede haber estados terminales permitidos. Coordinator no invoca ni inspecciona ningún sistema de control de versiones.

</details>

<details>
<summary>Inspección del estado, incluido Windows</summary>

Los documentos persistentes de flujos de trabajo con esquema v6 se guardan en `~/.agent-coordinator/workflows`. Estos comandos son de solo lectura y nunca crean, bloquean, reparan, normalizan, almacenan en caché ni limpian el estado.

```sh
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py list --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py status --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py context --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py next --workflow-id WORKFLOW --json
```

En el símbolo del sistema de Windows, usa `python` y la ruta del usuario actual:

```bat
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" list --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" status --workflow-id WORKFLOW --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" context --workflow-id WORKFLOW --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" next --workflow-id WORKFLOW --json
```

En Windows, el estado se guarda en `%USERPROFILE%\.agent-coordinator\workflows`.

</details>

<details>
<summary>Demostración con Docker</summary>

La demostración usa una imagen universal de OpenAI Codex fijada a una versión concreta y requiere `OPENAI_API_KEY` en el archivo `.env` ignorado, ubicado en la raíz. Los montajes de la skill y del código fuente son de solo lectura; la salida modificable permanece en el directorio ignorado `data/`.

Genera el backend con Coordinator:

```sh
docker compose run --rm coordinator
```

`data/project/` debe estar limpio; un archivo regular `.nvmrc` ya existente es la única entrada permitida en el nivel superior. Conserva todo lo que necesites antes de vaciar `data/` para otra ejecución.

La aplicación generada se encuentra en `data/project/`, con el backend en `data/project/backend/`. Las sesiones de Codex, el estado de Coordinator y los datos de SQLite usan directorios del mismo nivel, y la aplicación generada queda fuera de la verificación automatizada del repositorio.

Inicia el backend generado de forma manual:

```sh
docker compose up backend
```

La API estará disponible en `http://localhost:3000` y su base de datos SQLite persistirá en `data/sqlite/todos.db`.

</details>

## Proyecto

- [Licencia MIT](LICENSE)
- [Guía para contribuir](CONTRIBUTING.md)
- [Política de seguridad](SECURITY.md)
- [Repositorio de GitHub](https://github.com/alanhoff/agent-coordinator)
- [Seguimiento público de incidencias](https://github.com/alanhoff/agent-coordinator/issues)
