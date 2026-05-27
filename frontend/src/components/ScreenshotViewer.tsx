import { useEffect, useMemo, useRef, useState } from 'react';
import type { TrajectoryStep } from '../types/contracts';

interface ScreenshotViewerProps {
  runId: string;
  step: TrajectoryStep;
  totalSteps: number;
  detailsExpanded: boolean;
  onPrev: () => void;
  onNext: () => void;
}

export function ScreenshotViewer({ runId, step, totalSteps, detailsExpanded, onPrev, onNext }: ScreenshotViewerProps) {
  const [isPlaying, setIsPlaying] = useState(false);
  const [failedScreenshotUrl, setFailedScreenshotUrl] = useState<string | null>(null);
  const [stageSize, setStageSize] = useState<{ width: number; height: number } | null>(null);
  const [naturalSize, setNaturalSize] = useState<{ width: number; height: number } | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const screenshotUrl = step.observation.screenshot 
    ? `/api/runs/${runId}/screenshots/${step.observation.screenshot}`
    : null;
  const imageWidth = step.coordinate_validation.image_width;
  const imageHeight = step.coordinate_validation.image_height;
  const canOverlay = step.coordinate_validation.status === 'validated' && Boolean(imageWidth && imageHeight);
  const coordinates = step.action.coordinates;
  const showMarker = canOverlay && coordinates;
  const showBBox = canOverlay && isBBoxInBounds(step, imageWidth, imageHeight);
  const imageFailed = screenshotUrl !== null && failedScreenshotUrl === screenshotUrl;
  const fittedSize = useMemo(() => {
    const sourceWidth = naturalSize?.width ?? imageWidth;
    const sourceHeight = naturalSize?.height ?? imageHeight;
    if (!sourceWidth || !sourceHeight || !stageSize?.width || !stageSize.height) return null;
    return fitInside(sourceWidth, sourceHeight, stageSize.width, stageSize.height);
  }, [imageHeight, imageWidth, naturalSize, stageSize]);

  useEffect(() => {
    if (!isPlaying) return undefined;
    // step.index is 1-based, so the last step's index equals totalSteps.
    // Once we've landed on the final step, auto-stop so the play button
    // resets and a subsequent manual step-jump doesn't silently resume.
    if (step.index >= totalSteps) {
      setIsPlaying(false);
      return undefined;
    }
    const timer = window.setInterval(onNext, 1000);
    return () => window.clearInterval(timer);
  }, [isPlaying, onNext, step.index, totalSteps]);

  useEffect(() => {
    setNaturalSize(null);
  }, [screenshotUrl]);

  useEffect(() => {
    if (!stageRef.current) return undefined;
    const element = stageRef.current;
    const updateSize = () => {
      const rect = element.getBoundingClientRect();
      setStageSize({ width: rect.width, height: rect.height });
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const measureStage = () => {
      if (!stageRef.current) return;
      const rect = stageRef.current.getBoundingClientRect();
      setStageSize({ width: rect.width, height: rect.height });
    };
    const frame = window.requestAnimationFrame(measureStage);
    const timer = window.setTimeout(measureStage, 320);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
    };
  }, [detailsExpanded]);

  return (
    <section
      data-details-expanded={detailsExpanded}
      className="m-3 mb-2 flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm transition-[flex-basis,height] duration-300 ease-in-out"
    >
      <div className="flex shrink-0 items-center justify-between border-b border-slate-200 bg-slate-50/70 px-3 py-2">
        <div className="text-sm font-semibold text-slate-900">
          Step {step.index} <span className="font-normal text-slate-400">/ {totalSteps}</span>
          <span className="ml-3 font-normal text-slate-500">Screenshot (after action)</span>
        </div>
        <div className="flex items-center gap-1 text-slate-500">
          <button className="rounded-md border border-slate-200 bg-white p-1.5 hover:text-indigo-700" title="Zoom">
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="m21 21-4.35-4.35M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15Zm0-10v5m-2.5-2.5h5" />
            </svg>
          </button>
          <button className="rounded-md border border-slate-200 bg-white p-1.5 hover:text-indigo-700" title="Fit screenshot">
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3m18 0v3a2 2 0 0 1-2 2h-3" />
            </svg>
          </button>
        </div>
      </div>
      
      <div className="relative flex min-h-0 flex-1 overflow-hidden bg-slate-100 p-2">
        <div
          ref={stageRef}
          data-screenshot-stage
          className="flex min-h-0 flex-1 items-center justify-center overflow-hidden transition-[height] duration-300 ease-in-out"
        >
        {screenshotUrl && !imageFailed ? (
          <div
            className="relative max-h-full max-w-full overflow-hidden rounded-md bg-white shadow-inner"
            style={fittedSize ? { width: fittedSize.width, height: fittedSize.height } : undefined}
          >
            <img
              src={screenshotUrl}
              alt={`Step ${step.index}`}
              className={fittedSize ? 'block h-full w-full object-contain' : 'block max-h-full max-w-full object-contain'}
              onLoad={(event) => {
                setNaturalSize({
                  width: event.currentTarget.naturalWidth,
                  height: event.currentTarget.naturalHeight,
                });
              }}
              onError={() => setFailedScreenshotUrl(screenshotUrl)}
            />
            
            {showMarker && coordinates && imageWidth && imageHeight && (
              <div 
                className="pointer-events-none absolute h-7 w-7 rounded-full border-2 border-red-500 bg-red-500/10"
                style={{ 
                  left: `${(coordinates.x / imageWidth) * 100}%`, 
                  top: `${(coordinates.y / imageHeight) * 100}%`,
                  transform: 'translate(-50%, -50%)'
                }}
              >
                <span className="absolute inset-0 rounded-full border-2 border-red-500 opacity-60 animate-ping" />
              </div>
            )}
            
            {showBBox && step.action.bbox && imageWidth && imageHeight && (
              <div 
                className="pointer-events-none absolute rounded-sm border-2 border-red-500 bg-red-500/10"
                style={{
                  left: `${(step.action.bbox.x / imageWidth) * 100}%`,
                  top: `${(step.action.bbox.y / imageHeight) * 100}%`,
                  width: `${(step.action.bbox.width / imageWidth) * 100}%`,
                  height: `${(step.action.bbox.height / imageHeight) * 100}%`
                }}
              />
            )}
          </div>
        ) : (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white px-5 py-8 text-sm text-slate-500">
            Screenshot not available
          </div>
        )}
        </div>
      </div>

      <div className="flex shrink-0 items-center justify-between gap-4 border-t border-slate-200 bg-white px-3 py-2">
        <div className="flex items-center gap-1">
          <button onClick={onPrev} disabled={step.index <= 1} className="rounded-full p-2 text-slate-600 hover:bg-slate-100 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-30" title="Previous step">
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="m15 18-6-6 6-6" />
            </svg>
          </button>
          <button onClick={() => setIsPlaying((value) => step.index < totalSteps && !value)} className="rounded-full p-2 text-slate-600 hover:bg-slate-100 hover:text-indigo-700" title={isPlaying ? 'Pause playback' : 'Play steps'}>
            {isPlaying ? (
              <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 20 20">
                <path d="M6 4h2.5v12H6V4Zm5.5 0H14v12h-2.5V4Z" />
              </svg>
            ) : (
              <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 20 20">
                <path d="m7 4 9 6-9 6V4Z" />
              </svg>
            )}
          </button>
          <button onClick={onNext} disabled={step.index >= totalSteps} className="rounded-full p-2 text-slate-600 hover:bg-slate-100 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-30" title="Next step">
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="m9 18 6-6-6-6" />
            </svg>
          </button>
        </div>
        <div className="relative h-1.5 flex-1 rounded-full bg-slate-200">
          {/* step.index is 1-based; map onto [0%, 100%] across totalSteps - 1 intervals. */}
          <div className="absolute left-0 top-0 h-full rounded-full bg-red-400 transition-all" style={{ width: `${((step.index - 1) / (totalSteps - 1 || 1)) * 100}%` }} />
          <div className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-red-500 shadow" style={{ left: `${((step.index - 1) / (totalSteps - 1 || 1)) * 100}%` }} />
        </div>
        <div className="w-20 text-right text-xs font-medium text-slate-500">Step {step.index} / {totalSteps}</div>
      </div>
    </section>
  );
}

function fitInside(sourceWidth: number, sourceHeight: number, targetWidth: number, targetHeight: number) {
  const scale = Math.min(targetWidth / sourceWidth, targetHeight / sourceHeight);
  return {
    width: Math.max(1, sourceWidth * scale),
    height: Math.max(1, sourceHeight * scale),
  };
}

function isBBoxInBounds(step: TrajectoryStep, imageWidth?: number, imageHeight?: number) {
  const bbox = step.action.bbox;
  if (!bbox || !imageWidth || !imageHeight) return false;
  return (
    bbox.x >= 0 &&
    bbox.y >= 0 &&
    bbox.width > 0 &&
    bbox.height > 0 &&
    bbox.x + bbox.width <= imageWidth &&
    bbox.y + bbox.height <= imageHeight
  );
}
