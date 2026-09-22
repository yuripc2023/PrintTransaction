import logging

import servicemanager
import win32event
import win32service
import win32serviceutil

from print_server import LOG_FILE_PATH, init_service_stop_event, main, setup_logging


class PrintServerWindowsService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ATICPrintServer"
    _svc_display_name_ = "ATIC Print Server"
    _svc_description_ = "Consulta pedidos pendientes y los imprime por centro de produccion."

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        logging.info("Solicitud de parada recibida para el servicio Windows.")
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        setup_logging()
        init_service_stop_event(self.hWaitStop)
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        logging.info("Servicio Windows iniciado. Log principal: %s", LOG_FILE_PATH)
        try:
            main()
        except Exception as exc:
            logging.error("Error critico en el servicio Windows: %s", exc, exc_info=True)
            servicemanager.LogErrorMsg(f"{self._svc_name_} fallo: {exc}")
            raise
        finally:
            logging.info("Servicio Windows finalizado.")


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(PrintServerWindowsService)
