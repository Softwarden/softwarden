import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { DOCS } from './lib/docs-manifest';

/** The repository's docs, read in place from ../docs. Only the files listed in the manifest are loaded. */
const docs = defineCollection({
  loader: glob({
    base: '../docs',
    pattern: DOCS.map((d) => d.file),
    generateId: ({ entry }) => DOCS.find((d) => d.file === entry.replace(/\\/g, '/'))?.slug ?? entry,
  }),
});

/** The repository's CHANGELOG.md, read in place. */
const changelog = defineCollection({
  loader: glob({ base: '..', pattern: 'CHANGELOG.md', generateId: () => 'changelog' }),
});

export const collections = { docs, changelog };
