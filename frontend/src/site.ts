// Single source of truth for release data and site links.
// To ship a release: bump `release`, add src/content/changelog/<version>.md, push.

export const repo = "https://github.com/Mshahnawaz1/imagory";

export const release = {
  version: "0.2.0",
  tag: "v0.2.0", // must match the GitHub release tag
  date: "2026-11-12",
};

export type Os = "windows" | "linux";

export const downloads: Record<
  Os,
  { label: string; file: string; requirements: string; size: string }
> = {
  windows: {
    label: "Windows",
    file: "Imagory.exe",
    requirements: "Windows 10 and 11, 64-bit",
    size: "about 50 MB",
  },
  linux: {
    label: "Linux",
    file: `Imagory-${release.version}-linux-x86_64.tar.gz`,
    requirements: "x86_64, Ubuntu 22.04 or newer, X11 or XWayland",
    size: "about 50 MB",
  },
};

/** Direct link to a release file on GitHub. */
export const fileUrl = (os: Os) =>
  `${repo}/releases/download/${release.tag}/${downloads[os].file}`;

/** The site's own download page, which counts the download and then starts it. */
export const downloadPage = (os: Os) => `/download/${os}`;

export const links = {
  releases: `${repo}/releases`,
  issues: `${repo}/issues`,
  newIssue: `${repo}/issues/new`,
  licence: `${repo}/blob/main/LICENSE`,
  editDocs: `${repo}/edit/main/frontend/src/content/docs`,
};

export const nav = [
  { label: "Features", href: "/#features" },
  { label: "Docs", href: "/docs" },
  { label: "Changelog", href: "/changelog" },
  { label: "GitHub", href: repo },
];

export const footer = [
  {
    title: "Product",
    links: [
      { label: "Download", href: "/#download" },
      { label: "Features", href: "/#features" },
      { label: "Changelog", href: "/changelog" },
    ],
  },
  {
    title: "Docs",
    links: [
      { label: "Getting started", href: "/docs/getting-started" },
      { label: "Takeout cleanup", href: "/docs/takeout-cleanup" },
      { label: "Troubleshooting", href: "/docs/troubleshooting" },
      { label: "FAQ", href: "/docs/faq" },
    ],
  },
  {
    title: "Project",
    links: [
      { label: "GitHub", href: repo },
      { label: "Report a bug", href: links.newIssue },
      { label: "Licence (MIT)", href: links.licence },
      { label: "Privacy", href: "/privacy" },
    ],
  },
];
