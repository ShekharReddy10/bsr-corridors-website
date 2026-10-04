"use client";

import { useCallback, useEffect, useState } from "react";
import type { Photo } from "@/content/hotel";

export default function Gallery({ photos }: { photos: Photo[] }) {
  const [index, setIndex] = useState<number | null>(null);
  const close = useCallback(() => setIndex(null), []);
  const step = useCallback(
    (d: number) => setIndex((i) => (i === null ? i : (i + d + photos.length) % photos.length)),
    [photos.length],
  );

  useEffect(() => {
    if (index === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      if (e.key === "ArrowRight") step(1);
      if (e.key === "ArrowLeft") step(-1);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = "";
      window.removeEventListener("keydown", onKey);
    };
  }, [index, close, step]);

  return (
    <>
      <div className="gallery-grid">
        {photos.map((p, i) => (
          <button key={p.src} className={`gallery-item${p.tall ? " is-tall" : ""}`} onClick={() => setIndex(i)} aria-label={`View photo: ${p.alt}`}>
            <img src={p.src} alt={p.alt} loading="lazy" />
          </button>
        ))}
      </div>

      {index !== null && (
        <div className="lightbox" role="dialog" aria-modal="true" aria-label="Photo viewer" onClick={close}>
          <button className="lightbox-close" onClick={close} aria-label="Close">
            ×
          </button>
          {photos.length > 1 && (
            <button
              className="lightbox-nav prev"
              onClick={(e) => {
                e.stopPropagation();
                step(-1);
              }}
              aria-label="Previous photo"
            >
              ‹
            </button>
          )}
          <figure onClick={(e) => e.stopPropagation()}>
            <img src={photos[index].src} alt={photos[index].alt} />
            <figcaption>
              {index + 1} / {photos.length}
            </figcaption>
          </figure>
          {photos.length > 1 && (
            <button
              className="lightbox-nav next"
              onClick={(e) => {
                e.stopPropagation();
                step(1);
              }}
              aria-label="Next photo"
            >
              ›
            </button>
          )}
        </div>
      )}
    </>
  );
}
