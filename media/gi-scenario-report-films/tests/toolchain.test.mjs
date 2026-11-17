import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CAPTIONS_PATH,
  FILMS_PATH,
  captionsToSrt,
  parseCli,
  readJson,
  validateCaptions,
  validateFilmRecord,
} from "../scripts/toolchain.mjs";

test("all five films satisfy the authored render contract", async () => {
  const production = await readJson(FILMS_PATH);
  assert.equal(production.width, 1920);
  assert.equal(production.height, 1080);
  assert.equal(production.fps, 30);
  assert.equal(production.films.length, 5);
  assert.equal(new Set(production.films.map((film) => film.key)).size, 5);
  assert.equal(new Set(production.films.map((film) => film.slug)).size, 5);
  for (const film of production.films) assert.equal(validateFilmRecord(film, production), true);
});

test("captions cover each complete film without overlap", async () => {
  const production = await readJson(FILMS_PATH);
  const captions = await readJson(CAPTIONS_PATH);
  for (const film of production.films) {
    const cues = captions.films[film.slug];
    assert.equal(validateCaptions(film, cues), true);
    const srt = captionsToSrt(cues);
    assert.match(srt, /^1\n00:00:00,000 -->/);
    assert.match(srt, new RegExp(`${String(film.duration).padStart(2, "0")},000`));
  }
});

test("CLI parser fails closed on unknown arguments", () => {
  assert.deepEqual(parseCli(["--stills-only", "--film", "decision-funnel"]), {
    stillsOnly: true,
    film: "decision-funnel",
  });
  assert.throws(() => parseCli(["--wat"]), /Unknown argument/);
  assert.throws(() => parseCli(["--film"]), /requires a film slug/);
});
