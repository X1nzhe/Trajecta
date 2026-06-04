import { useState, useEffect, useCallback } from 'react';

interface UrlState {
  trajectoryId: string | null;
  stepIndex: number | null;
}

export function useUrlState() {
  const [state, setState] = useState<UrlState>(() => {
    const params = new URLSearchParams(window.location.search);
    const trajectoryId = params.get('trajectory');
    const step = params.get('step');
    return {
      trajectoryId: trajectoryId || null,
      stepIndex: step ? parseInt(step, 10) : null,
    };
  });

  useEffect(() => {
    const handlePopState = () => {
      const params = new URLSearchParams(window.location.search);
      const trajectoryId = params.get('trajectory');
      const step = params.get('step');
      setState({
        trajectoryId: trajectoryId || null,
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
      if (nextState.trajectoryId) params.set('trajectory', nextState.trajectoryId);
      if (nextState.stepIndex !== null) params.set('step', nextState.stepIndex.toString());
      
      const newUrl = params.toString() ? `?${params.toString()}` : window.location.pathname;
      window.history.pushState({}, '', newUrl);
      
      return nextState;
    });
  }, []);

  const setTrajectoryId = useCallback((trajectoryId: string | null) => {
    updateUrl({ trajectoryId, stepIndex: null }); // Reset step when trajectory changes
  }, [updateUrl]);

  const setStepIndex = useCallback((stepIndex: number | null) => {
    updateUrl({ stepIndex });
  }, [updateUrl]);

  return {
    trajectoryId: state.trajectoryId,
    stepIndex: state.stepIndex,
    setTrajectoryId,
    setStepIndex,
  };
}
