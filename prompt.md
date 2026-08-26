$coordinator Build a backend-only to-do application in `/workspace/backend` using Node.js
and Node's built-in `node:sqlite` library. Do not build a frontend. The delivered project must have
an `npm start` script, nonempty JavaScript modules under `src/`, and must store a real SQLite database
at the path in `TODO_DB_PATH`. `GET /health` must return 200 JSON.
The database must use a `todos` table with at least an integer `id` column and text `title` column.
`POST /api/todos` must accept `{ "title": string }` and return 201 JSON as
`{ "todo": { "id": integer, "title": string } }`; `GET /api/todos/:id` must return 200 with the same
envelope, including after a server restart.
Documentation alone is not a completed backend. Do not create or run automated tests; the generated
project is for manual evaluation only.
