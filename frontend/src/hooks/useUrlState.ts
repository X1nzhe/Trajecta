import { useState, useEffect, useCallback } from 'react';

interface UrlState {
  runId: string | null;
  stepIndex: number | null;
}

export function useUrlState() {
  const [state, setState] = useState<UrlState>(() => {
    const params = new URLSearchParams(window.location.search);
    const runId = params.get('run');
    const step = params.get('step');
    return {
      runId: runId || null,
      stepIndex: step ? parseInt(step, 10) : null,
    };
  });

  useEffect(() => {
    const handlePopState = () => {
      const params = new URLSearchParams(window.location.search);
      const runId = params.get('run');
      const step = params.get('step');
      setState({
        runId: runId || null,
        stepIndex: step ? parseInt(step, 10) : null,
      });
    };

    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  const updateUrl = useCallback((updates: Partial<UrlState>) => {
    setState((prev) => {
      const nextState = { ...prev, ...updates };
      const params = new URLSearchParams();
      if (nextState.runId) params.set('run', nextState.runId);
      if (nextState.stepIndex !== null) params.set('step', nextState.stepIndex.toString());
      
      const newUrl = params.toString() ? `?${params.toString()}` : window.location.pathname;
      window.history.pushState({}, '', newUrl);
      
      return nextState;
    });
  }, []);

  const setRunId = useCallback((runId: string | null) => {
    updateUrl({ runId, stepIndex: null }); // Reset step when run changes
  }, [updateUrl]);

  const setStepIndex = useCallback((stepIndex: number | null) => {
    updateUrl({ stepIndex });
  }, [updateUrl]);

  return {
    runId: state.runId,
    stepIndex: state.stepIndex,
    setRunId,
    setStepIndex,
  };
}
