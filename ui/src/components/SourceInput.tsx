import { FileText, FileUp, Link2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

export type Source = { kind: "file"; file: File } | { kind: "http"; url: string };

interface Props {
  source: Source | null;
  onChange: (source: Source | null) => void;
  maxFileSize?: number | null;
}

type InputMode = "file" | "http" | "gdoc";

const GOOGLE_URL_PATTERN = /^https?:\/\/(docs|drive)\.google\.com\//i;
const PUBLISHED_GDOC_PATTERN = /\/d\/e\/[^/]+\/pub/i;

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

/** Inline warning for a pasted link the server will reject. */
function gdocWarning(value: string): string | null {
  const link = value.trim();
  if (!link) return null;
  if (PUBLISHED_GDOC_PATTERN.test(link)) {
    return "This is a published-to-web link. Use the document's plain share link (…/d/<id>/edit) instead.";
  }
  if (link.includes("resourcekey=")) {
    return "This link is resource-key protected. Copy the plain share link (without resourcekey) instead.";
  }
  return null;
}

export function SourceInput({ source, onChange, maxFileSize }: Props) {
  const [mode, setMode] = useState<InputMode>(source?.kind ?? "file");
  const [url, setUrl] = useState(source?.kind === "http" ? source.url : "");
  const [gdocUrl, setGdocUrl] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  // Echoes of our own onChange must not snap the active tab back to the kind
  // carried by the emitted source (a Google Doc link is still `http`).
  const emitted = useRef<Source | null>(null);

  const emit = (next: Source | null) => {
    emitted.current = next;
    onChange(next);
  };

  // Follow sources set from outside (example links, files dropped on the page).
  useEffect(() => {
    if (!source || source === emitted.current) return;
    setMode(source.kind);
    if (source.kind === "http") setUrl(source.url);
  }, [source]);
  const file = source?.kind === "file" ? source.file : null;
  const tooLarge = file && maxFileSize ? file.size > maxFileSize : false;

  const pick = (files: FileList | null) => {
    const picked = files?.[0];
    if (picked) emit({ kind: "file", file: picked });
  };

  const selectMode = (next: InputMode) => {
    setMode(next);
    if (next === "http") {
      emit(url.trim() ? { kind: "http", url: url.trim() } : null);
    } else if (next === "gdoc") {
      emit(gdocUrl.trim() ? { kind: "http", url: gdocUrl.trim() } : null);
    } else {
      emit(null);
    }
  };

  const gdocValue = gdocUrl.trim();
  const warning = gdocWarning(gdocUrl);
  const googleUrl = GOOGLE_URL_PATTERN.test(url.trim());

  return (
    <Tabs
      value={mode}
      onValueChange={(value) => selectMode(value as InputMode)}
    >
      <TabsList className="grid w-full grid-cols-3">
        <TabsTrigger value="file">
          <FileUp className="size-4" /> File
        </TabsTrigger>
        <TabsTrigger value="http">
          <Link2 className="size-4" /> URL
        </TabsTrigger>
        <TabsTrigger value="gdoc">
          <FileText className="size-4" /> Google Doc
        </TabsTrigger>
      </TabsList>

      <TabsContent value="file">
        <div
          role="button"
          tabIndex={0}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") inputRef.current?.click();
          }}
          className="flex min-h-28 cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed border-input p-4 text-center transition-colors hover:border-brand/60 hover:bg-muted"
        >
          {file ? (
            <div className="flex w-full items-center justify-between gap-2">
              <div className="min-w-0 text-left">
                <p className="truncate text-sm font-medium">{file.name}</p>
                <p className={cn("text-xs", tooLarge ? "text-destructive" : "text-muted-foreground")}>
                  {formatBytes(file.size)}
                  {tooLarge && maxFileSize ? ` — larger than the server limit of ${formatBytes(maxFileSize)}` : ""}
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Remove file"
                onClick={(event) => {
                  event.stopPropagation();
                  emit(null);
                  if (inputRef.current) inputRef.current.value = "";
                }}
              >
                <X className="size-4" />
              </Button>
            </div>
          ) : (
            <>
              <FileUp className="size-6 text-brand" />
              <p className="text-sm font-medium">Drop a document anywhere or click to browse</p>
              <p className="text-xs text-muted-foreground">
                PDF, DOCX, PPTX, HTML, images and more, plus Google
                Docs/Sheets/Slides shortcuts (.gdoc/.gsheet/.gslides) — the linked
                document is fetched and converted
                {maxFileSize ? ` · up to ${formatBytes(maxFileSize)}` : ""}
              </p>
            </>
          )}
          <input ref={inputRef} type="file" className="hidden" onChange={(e) => pick(e.target.files)} />
        </div>
      </TabsContent>

      <TabsContent value="http">
        <Input
          type="url"
          placeholder="https://arxiv.org/pdf/2408.09869"
          value={url}
          onChange={(event) => {
            const next = event.target.value;
            setUrl(next);
            emit(next.trim() ? { kind: "http", url: next.trim() } : null);
          }}
        />
        {googleUrl && (
          <p className="mt-2 text-xs text-muted-foreground">
            Google Docs, Sheets and Slides links are exported as docx/xlsx/pptx before
            conversion.
          </p>
        )}
      </TabsContent>

      <TabsContent value="gdoc">
        <Input
          type="url"
          placeholder="https://docs.google.com/document/d/…/edit"
          value={gdocUrl}
          onChange={(event) => {
            const next = event.target.value;
            setGdocUrl(next);
            emit(next.trim() ? { kind: "http", url: next.trim() } : null);
          }}
        />
        <p className="mt-2 text-xs text-muted-foreground">
          The document must be shared: Anyone with the link → Viewer.
        </p>
        {warning && <p className="mt-1 text-xs text-destructive">{warning}</p>}
        {!warning && gdocValue && (
          <p className="mt-1 text-xs text-muted-foreground">
            Fetched from Google as docx/xlsx/pptx, then converted.
          </p>
        )}
      </TabsContent>
    </Tabs>
  );
}
