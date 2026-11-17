-- ============================================================================
-- Respaldo de Automatización SQL - Sistema IoT de Transporte Público
-- Motor: PostgreSQL (Supabase)
-- ============================================================================

-- 1. Función para sincronizar el estado en tiempo real de la unidad
CREATE OR REPLACE FUNCTION public.actualizar_estado_camion()
RETURNS TRIGGER AS $$
BEGIN
  UPDATE public.unidades
  SET 
    ultima_parada_id = NEW.parada_id,
    pasajeros_actuales = NEW.pasajeros_totales
  WHERE id = NEW.unidad_id;
  
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 2. Disparador ejecutado automáticamente tras cada inserción en registros_ocupacion
DROP TRIGGER IF EXISTS trigger_actualizar_camion ON public.registros_ocupacion;

CREATE TRIGGER trigger_actualizar_camion
AFTER INSERT ON public.registros_ocupacion
FOR EACH ROW
EXECUTE FUNCTION public.actualizar_estado_camion();
