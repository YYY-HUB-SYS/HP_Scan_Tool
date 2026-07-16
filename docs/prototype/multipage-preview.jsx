import { useState, useRef, useCallback } from "react";

// Multi-page batch preview prototype — throwaway, answers: "What should batch preview look like?"

const PAGES = Array.from({ length: 8 }, (_, i) => ({
  id: i + 1,
  thumb: `https://picsum.photos/seed/scan${i + 1}/160/200`,
  full: `https://picsum.photos/seed/scan${i + 1}/800/1000`,
}));

const SPLIT_COLOR = "#3B8ED0";

export default function MultiPagePreviewPrototype() {
  const [pages, setPages] = useState(PAGES);
  const [selected, setSelected] = useState(0);
  const [splits, setSplits] = useState(new Set()); // indices AFTER which a split is placed
  const [expMode, setExpMode] = useState("off");
  const [brightness, setBrightness] = useState(0);
  const [contrast, setContrast] = useState(0);
  const [groupNames, setGroupNames] = useState({});

  const deletePage = (idx) => {
    setPages((p) => p.filter((_, i) => i !== idx));
    setSplits((s) => {
      const next = new Set();
      s.forEach((i) => {
        if (i < idx) next.add(i);
        else if (i > idx) next.add(i - 1);
      });
      return next;
    });
    setSelected((sel) => Math.min(sel, pages.length - 2));
  };

  const toggleSplit = (idx) => {
    setSplits((s) => {
      const next = new Set(s);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  const groups = useCallback(() => {
    const result = [];
    let start = 0;
    for (let i = 0; i < pages.length; i++) {
      if (splits.has(i)) {
        result.push({ pages: pages.slice(start, i + 1), name: groupNames[start] || "" });
        start = i + 1;
      }
    }
    if (start < pages.length) {
      result.push({ pages: pages.slice(start), name: groupNames[start] || "" });
    }
    return result;
  }, [pages, splits, groupNames]);

  const filterStyle = {
    filter: `brightness(${100 + brightness}%) contrast(${100 + contrast}%)`,
  };

  const gs = groups();

  return (
    <div className="bg-gray-900 text-white min-h-screen p-4 font-sans">
      <div className="max-w-4xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <h1 className="text-xl font-bold">Multi-Page Preview Prototype</h1>
          <span className="text-sm text-gray-400">
            {pages.length} pages, {gs.length} group{gs.length !== 1 ? "s" : ""}
          </span>
        </div>

        {/* Main preview area */}
        <div className="bg-gray-800 rounded-lg p-4 mb-4">
          <div className="flex justify-center">
            <img
              src={pages[selected]?.full}
              alt={`Page ${selected + 1}`}
              className="max-h-96 rounded shadow-lg"
              style={filterStyle}
            />
          </div>
          <div className="text-center mt-2 text-sm text-gray-400">
            Page {selected + 1} of {pages.length}
            {splits.has(selected) && (
              <span className="ml-2 px-2 py-0.5 bg-blue-600 rounded text-white text-xs">
                SPLIT
              </span>
            )}
          </div>
        </div>

        {/* Thumbnail strip with split markers */}
        <div className="bg-gray-800 rounded-lg p-3 mb-4">
          <div className="text-xs text-gray-500 mb-2">
            Click between thumbnails to add/remove split markers
          </div>
          <div className="flex items-center gap-1 overflow-x-auto pb-2">
            {pages.map((page, idx) => (
              <div key={page.id} className="flex items-center">
                {/* Split marker BEFORE this thumbnail (between prev and this) */}
                {idx > 0 && (
                  <button
                    onClick={() => toggleSplit(idx - 1)}
                    className={`mx-0.5 w-3 h-12 rounded-sm transition-colors ${
                      splits.has(idx - 1)
                        ? "bg-blue-500"
                        : "bg-gray-600 hover:bg-gray-500"
                    }`}
                    title={splits.has(idx - 1) ? "Remove split" : "Add split"}
                  />
                )}
                {/* Thumbnail */}
                <div className="relative group">
                  <button
                    onClick={() => setSelected(idx)}
                    className={`w-16 h-20 rounded overflow-hidden border-2 transition-all ${
                      selected === idx
                        ? "border-blue-500 scale-105"
                        : "border-transparent hover:border-gray-500"
                    }`}
                  >
                    <img
                      src={page.thumb}
                      alt={`Page ${idx + 1}`}
                      className="w-full h-full object-cover"
                      style={filterStyle}
                    />
                  </button>
                  {/* Page number */}
                  <span className="absolute bottom-0 left-0 right-0 bg-black/60 text-center text-xs py-0.5">
                    {idx + 1}
                  </span>
                  {/* Delete button */}
                  <button
                    onClick={() => deletePage(idx)}
                    className="absolute -top-1 -right-1 w-5 h-5 bg-red-500 rounded-full text-xs opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center"
                  >
                    x
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Group summary */}
        {gs.length > 1 && (
          <div className="bg-gray-800 rounded-lg p-3 mb-4">
            <div className="text-xs text-gray-500 mb-2">
              Output files (split groups)
            </div>
            <div className="space-y-1">
              {gs.map((g, gi) => (
                <div key={gi} className="flex items-center gap-2 text-sm">
                  <span className="text-gray-400 w-16">
                    {g.pages.length}p
                  </span>
                  <input
                    className="bg-gray-700 rounded px-2 py-1 text-sm flex-1 placeholder-gray-500"
                    placeholder={`Group ${gi + 1} (optional name)`}
                    value={g.name}
                    onChange={(e) =>
                      setGroupNames((n) => ({
                        ...n,
                        [pages.indexOf(g.pages[0])]: e.target.value,
                      }))
                    }
                  />
                  <span className="text-gray-500 text-xs">
                    pp. {pages.indexOf(g.pages[0]) + 1}-
                    {pages.indexOf(g.pages[g.pages.length - 1]) + 1}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Exposure controls */}
        <div className="bg-gray-800 rounded-lg p-3 mb-4">
          <div className="text-sm font-bold mb-2">Exposure</div>
          <div className="flex gap-2 mb-3">
            {["off", "auto", "manual"].map((m) => (
              <button
                key={m}
                onClick={() => setExpMode(m)}
                className={`px-3 py-1 rounded text-sm ${
                  expMode === m
                    ? "bg-blue-600 text-white"
                    : "bg-gray-700 text-gray-300"
                }`}
              >
                {m === "off" ? "Off" : m === "auto" ? "Auto" : "Manual"}
              </button>
            ))}
            <span className="text-xs text-gray-500 self-center ml-2">
              (applied to {expMode === "off" ? "no" : "all"} pages)
            </span>
          </div>
          {expMode === "manual" && (
            <div className="grid grid-cols-2 gap-3">
              <label className="text-sm">
                Brightness: {brightness}
                <input
                  type="range"
                  min={-100}
                  max={100}
                  value={brightness}
                  onChange={(e) => setBrightness(Number(e.target.value))}
                  className="w-full"
                />
              </label>
              <label className="text-sm">
                Contrast: {contrast}
                <input
                  type="range"
                  min={-100}
                  max={100}
                  value={contrast}
                  onChange={(e) => setContrast(Number(e.target.value))}
                  className="w-full"
                />
              </label>
            </div>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex justify-between">
          <button className="px-4 py-2 bg-gray-700 rounded hover:bg-gray-600">
            Rescan
          </button>
          <div className="flex gap-2">
            <button
              onClick={() => {
                const summary = gs
                  .map(
                    (g, i) =>
                      `Group ${i + 1}: ${g.pages.length} pages${
                        g.name ? ` "${g.name}"` : ""
                      }`
                  )
                  .join("\n");
                alert(`Save ${pages.length} pages as ${gs.length} file(s):\n\n${summary}`);
              }}
              className="px-6 py-2 bg-blue-600 rounded font-bold hover:bg-blue-500"
            >
              Save ({gs.length} file{gs.length !== 1 ? "s" : ""})
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
