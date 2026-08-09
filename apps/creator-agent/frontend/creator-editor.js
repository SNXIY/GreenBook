import { Editor } from "@tiptap/core";
import CharacterCount from "@tiptap/extension-character-count";
import Placeholder from "@tiptap/extension-placeholder";
import StarterKit from "@tiptap/starter-kit";
import DOMPurify from "dompurify";
import {
  Activity,
  Bold,
  BookOpen,
  Check,
  ChevronDown,
  ClipboardCheck,
  Copy,
  Download,
  Expand,
  FileText,
  FolderKanban,
  GitBranch,
  Heading2,
  Italic,
  LibraryBig,
  Link2,
  List,
  ListOrdered,
  LogOut,
  Menu,
  MessageSquareText,
  MoreHorizontal,
  PanelLeft,
  PanelRight,
  Plus,
  Quote,
  Redo2,
  RotateCcw,
  Save,
  Scissors,
  Search,
  ShieldCheck,
  Sparkles,
  Send,
  ThumbsDown,
  ThumbsUp,
  Undo2,
  Upload,
  UserRound,
  WandSparkles,
  X,
  createIcons
} from "lucide";
import { marked } from "marked";
import TurndownService from "turndown";

const ICONS = {
  Activity,
  Bold,
  BookOpen,
  Check,
  ChevronDown,
  ClipboardCheck,
  Copy,
  Download,
  Expand,
  FileText,
  FolderKanban,
  GitBranch,
  Heading2,
  Italic,
  LibraryBig,
  Link2,
  List,
  ListOrdered,
  LogOut,
  Menu,
  MessageSquareText,
  MoreHorizontal,
  PanelLeft,
  PanelRight,
  Plus,
  Quote,
  Redo2,
  RotateCcw,
  Save,
  Scissors,
  Search,
  ShieldCheck,
  Sparkles,
  Send,
  ThumbsDown,
  ThumbsUp,
  Undo2,
  Upload,
  UserRound,
  WandSparkles,
  X
};

const ALLOWED_TAGS = [
  "p",
  "br",
  "strong",
  "em",
  "s",
  "h2",
  "h3",
  "h4",
  "ul",
  "ol",
  "li",
  "blockquote",
  "pre",
  "code",
  "hr",
  "a"
];

function refreshIcons(root = document) {
  createIcons({
    attrs: {
      "aria-hidden": "true",
      "stroke-width": 1.8
    },
    icons: ICONS,
    root
  });
}

function markdownToHtml(markdown) {
  const rendered = marked.parse(String(markdown || ""), {
    async: false,
    breaks: false,
    gfm: true
  });
  return DOMPurify.sanitize(String(rendered), {
    ALLOWED_ATTR: ["href", "rel", "target"],
    ALLOWED_TAGS
  });
}

function createMindFlowEditor({
  element,
  toolbar,
  onSelectionChange = () => {},
  onUpdate = () => {}
}) {
  const turndown = new TurndownService({
    bulletListMarker: "-",
    codeBlockStyle: "fenced",
    emDelimiter: "*",
    headingStyle: "atx",
    strongDelimiter: "**"
  });
  turndown.keep(["s"]);
  let applyingContent = false;

  const editor = new Editor({
    element,
    extensions: [
      StarterKit.configure({
        heading: { levels: [2, 3, 4] }
      }),
      Placeholder.configure({
        placeholder: "正文会出现在这里。你也可以从空白处开始写。"
      }),
      CharacterCount
    ],
    content: "",
    editorProps: {
      attributes: {
        class: "mindflow-prose",
        spellcheck: "true"
      }
    },
    onCreate: () => {
      updateToolbar();
      emitSelection();
    },
    onSelectionUpdate: () => {
      updateToolbar();
      emitSelection();
    },
    onTransaction: updateToolbar,
    onUpdate: ({ editor: current }) => {
      if (applyingContent) return;
      onUpdate({
        characters: current.storage.characterCount.characters(),
        markdown: turndown.turndown(current.getHTML()),
        words: current.storage.characterCount.words()
      });
    }
  });

  function selectedText() {
    const { from, to } = editor.state.selection;
    return from === to ? "" : editor.state.doc.textBetween(from, to, " ");
  }

  function emitSelection() {
    const text = selectedText();
    const markdown = turndown.turndown(editor.getHTML());
    const first = text ? markdown.indexOf(text) : -1;
    const unique = first >= 0 && markdown.indexOf(text, first + text.length) < 0;
    onSelectionChange({
      empty: editor.state.selection.empty,
      text,
      prefixContext: unique ? markdown.slice(Math.max(0, first - 500), first) : "",
      suffixContext: unique
        ? markdown.slice(first + text.length, first + text.length + 500)
        : ""
    });
  }

  function updateToolbar() {
    if (!toolbar) return;
    const active = {
      bold: editor.isActive("bold"),
      italic: editor.isActive("italic"),
      heading2: editor.isActive("heading", { level: 2 }),
      bulletList: editor.isActive("bulletList"),
      orderedList: editor.isActive("orderedList"),
      blockquote: editor.isActive("blockquote")
    };
    toolbar.querySelectorAll("[data-editor-command]").forEach((button) => {
      button.classList.toggle(
        "is-active",
        Boolean(active[button.dataset.editorCommand])
      );
    });
  }

  function runCommand(command) {
    const chain = editor.chain().focus();
    switch (command) {
      case "bold":
        chain.toggleBold().run();
        break;
      case "italic":
        chain.toggleItalic().run();
        break;
      case "heading2":
        chain.toggleHeading({ level: 2 }).run();
        break;
      case "bulletList":
        chain.toggleBulletList().run();
        break;
      case "orderedList":
        chain.toggleOrderedList().run();
        break;
      case "blockquote":
        chain.toggleBlockquote().run();
        break;
      case "undo":
        chain.undo().run();
        break;
      case "redo":
        chain.redo().run();
        break;
      default:
        break;
    }
  }

  toolbar?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-editor-command]");
    if (!button) return;
    runCommand(button.dataset.editorCommand);
  });

  return {
    destroy() {
      editor.destroy();
    },
    focus() {
      editor.commands.focus();
    },
    getMarkdown() {
      return turndown.turndown(editor.getHTML());
    },
    getSelectedText: selectedText,
    findText(text) {
      const target = String(text || "");
      if (!target) return false;
      let match = null;
      editor.state.doc.descendants((current, position) => {
        if (match || !current.isText || !current.text) return;
        const index = current.text.indexOf(target);
        if (index >= 0) {
          match = {
            from: position + index,
            to: position + index + target.length
          };
        }
      });
      if (!match) return false;
      editor
        .chain()
        .focus()
        .setTextSelection(match)
        .scrollIntoView()
        .run();
      return true;
    },
    isEmpty() {
      return editor.isEmpty;
    },
    replaceSelection(markdown) {
      editor
        .chain()
        .focus()
        .insertContent(markdownToHtml(markdown))
        .run();
    },
    setEditable(editable) {
      editor.setEditable(Boolean(editable));
    },
    setMarkdown(markdown) {
      applyingContent = true;
      editor.commands.setContent(markdownToHtml(markdown), {
        emitUpdate: false
      });
      applyingContent = false;
      updateToolbar();
      emitSelection();
    },
    wordCount() {
      return {
        characters: editor.storage.characterCount.characters(),
        words: editor.storage.characterCount.words()
      };
    }
  };
}

window.MindFlowEditor = {
  create: createMindFlowEditor,
  refreshIcons
};

document.addEventListener("DOMContentLoaded", () => refreshIcons());
