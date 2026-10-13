type PluginContext = {
  addRibbonIcon?: (icon: string, title: string, callback: () => void) => void;
  addCommand?: (command: { id: string; name: string; callback: () => void }) => void;
  registerMarkdownCodeBlockProcessor?: (
    language: string,
    processor: (source: string, element: HTMLElement) => void,
  ) => void;
};

type PadosSettings = {
  apiBaseUrl: string;
};

const DEFAULT_SETTINGS: PadosSettings = {
  apiBaseUrl: "http://127.0.0.1:8088",
};

function assertLocalhost(url: string): void {
  const parsed = new URL(url);
  if (parsed.hostname !== "127.0.0.1" && parsed.hostname !== "localhost") {
    throw new Error("PADOS API must be localhost");
  }
}

export class PadosControlSurfacePlugin {
  settings: PadosSettings = DEFAULT_SETTINGS;
  status = "offline";

  async onload(context?: PluginContext): Promise<void> {
    assertLocalhost(this.settings.apiBaseUrl);
    context?.addRibbonIcon?.("blocks", "Refresh PADOS projections", () => {
      void this.refresh();
    });
    context?.addCommand?.({
      id: "pados-refresh-projections",
      name: "Refresh PADOS projections",
      callback: () => {
        void this.refresh();
      },
    });
    context?.registerMarkdownCodeBlockProcessor?.("pados-control", (_source, element) => {
      element.textContent = `PADOS ${this.status}`;
    });
  }

  async refresh(): Promise<void> {
    assertLocalhost(this.settings.apiBaseUrl);
    try {
      const response = await fetch(`${this.settings.apiBaseUrl}/control/health`);
      this.status = response.ok ? "online" : "offline";
    } catch {
      this.status = "offline";
    }
  }
}

export default PadosControlSurfacePlugin;
