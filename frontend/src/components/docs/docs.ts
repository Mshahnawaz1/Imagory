import { getCollection, type CollectionEntry } from "astro:content";
import { docGroups } from "../../content.config";

export type Doc = CollectionEntry<"docs">;

/** All docs in reading order (group, then order). */
export async function sortedDocs(): Promise<Doc[]> {
  const all = await getCollection("docs");
  return all.sort(
    (a, b) =>
      docGroups.indexOf(a.data.group) - docGroups.indexOf(b.data.group) ||
      a.data.order - b.data.order,
  );
}

/** Docs split into sidebar groups. */
export async function groupedDocs() {
  const all = await sortedDocs();
  return docGroups.map((name) => ({ name, items: all.filter((d) => d.data.group === name) }));
}
