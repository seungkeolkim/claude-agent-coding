/**
 * Playwright 설정 — Agent Hub E2E 컨테이너 전용
 *
 * 환경변수로 런타임 동작을 조절한다. 기본값은 project 설계서(§5.5)와 일치.
 *   BROWSER         chromium | firefox | webkit (기본 chromium — MCP Docker 지원)
 *   BASE_URL        테스트 대상 URL (예: http://localhost:3000)
 *   VIEWPORT_W      뷰포트 가로 (기본 1280)
 *   VIEWPORT_H      뷰포트 세로 (기본 720)
 *   SCREENSHOTS     on | off | only-on-failure (기본 only-on-failure)
 *   VIDEO           on | off | retain-on-failure (기본 off)
 *   TRACE           on | off | retain-on-failure (기본 retain-on-failure)
 *   RETRIES         Playwright 자체 retry 횟수 (기본 0 — §4.6-3)
 */
import { defineConfig } from "@playwright/test";

// 환경변수를 Playwright enum 타입으로 안전하게 변환
function screenshotOption(value: string | undefined): "on" | "off" | "only-on-failure" {
  if (value === "on" || value === "only-on-failure") return value;
  return "off";
}

function videoOption(value: string | undefined): "on" | "off" | "retain-on-failure" {
  if (value === "on" || value === "retain-on-failure") return value;
  return "off";
}

function traceOption(value: string | undefined): "on" | "off" | "retain-on-failure" | "on-first-retry" {
  if (value === "on" || value === "retain-on-failure" || value === "on-first-retry") return value;
  return "off";
}

const browserName = (process.env.BROWSER || "chromium") as "chromium" | "firefox" | "webkit";
const viewportWidth = parseInt(process.env.VIEWPORT_W || "1280", 10);
const viewportHeight = parseInt(process.env.VIEWPORT_H || "720", 10);
const retries = parseInt(process.env.RETRIES || "0", 10);

export default defineConfig({
  testDir: "/e2e/tests",
  outputDir: "/e2e/test-results",
  fullyParallel: false,              // 같은 브라우저/세션 내 순차 실행
  retries: retries,                  // §4.6-3 기본 0. 호스트 config.yaml의 tester.retry_count로 주입
  reporter: [
    ["json", { outputFile: "/e2e/test-results/report.json" }],
    ["list"],
  ],
  use: {
    baseURL: process.env.BASE_URL || undefined,
    browserName: browserName,
    headless: true,
    viewport: { width: viewportWidth, height: viewportHeight },
    screenshot: screenshotOption(process.env.SCREENSHOTS || "only-on-failure"),
    video: videoOption(process.env.VIDEO),
    trace: traceOption(process.env.TRACE || "retain-on-failure"),
  },
});
