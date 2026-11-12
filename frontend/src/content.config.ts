import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

export const docGroups = ["Getting started", "Using Imagory", "Help"] as const;

const docs = defineCollection({
  loader: glob({ pattern: "*.md", base: "./src/content/docs" }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    group: z.enum(docGroups),
    order: z.number(),
  }),
});

const changelog = defineCollection({
  loader: glob({ pattern: "*.md", base: "./src/content/changelog" }),
  schema: z.object({
    version: z.string(),
    date: z.coerce.date(),
    title: z.string().optional(),
  }),
});

export const collections = { docs, changelog };
