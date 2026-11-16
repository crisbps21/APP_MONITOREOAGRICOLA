import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:http/http.dart' as http;
import 'package:idb_shim/idb.dart' as idb;
import 'package:idb_shim/idb_browser.dart' show idbFactoryBrowser;
import 'package:image_picker/image_picker.dart';
import 'package:latlong2/latlong.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';

// ---------------------------------------------------------------------------
// CONFIGURACIÓN GLOBAL
// ---------------------------------------------------------------------------

/// Dirección del servidor (backend).
///
/// - Si abres la app desde la misma computadora: http://127.0.0.1:8000
/// - Si la abres desde el celular, la app usa solo la IP desde la que se
///   cargó la página (por ejemplo http://192.168.1.50:8000).
/// - También se puede cambiar a mano con el botón de servidor.
class Servidor {
  static const String _clave = 'servidor_url';
  static String? _guardada;

  static String get porDefecto {
    try {
      final base = Uri.base;

      // Servida por https (túnel o nube) o por el propio backend (puerto
      // 8000): el servidor es la misma dirección desde la que se abrió.
      if (base.scheme == 'https' || (base.hasPort && base.port == 8000)) {
        return base.origin;
      }

      if (base.host.isNotEmpty &&
          base.host != 'localhost' &&
          base.host != '127.0.0.1') {
        return 'http://${base.host}:8000';
      }
    } catch (_) {}

    return 'http://127.0.0.1:8000';
  }

  static String get url {
    final guardada = _guardada;

    if (guardada == null) return porDefecto;

    // Una dirección http guardada no sirve si la app se abrió por https
    // (el navegador la bloquea): se usa la automática.
    try {
      if (Uri.base.scheme == 'https' && guardada.startsWith('http://')) {
        return porDefecto;
      }
    } catch (_) {}

    return guardada;
  }

  static Future<void> cargar() async {
    final prefs = await SharedPreferences.getInstance();
    _guardada = prefs.getString(_clave);
  }

  static String normalizar(String texto) {
    var valor = texto.trim();

    if (valor.isEmpty) return '';

    if (!valor.startsWith('http://') && !valor.startsWith('https://')) {
      valor = 'http://$valor';
    }

    while (valor.endsWith('/')) {
      valor = valor.substring(0, valor.length - 1);
    }

    final uri = Uri.tryParse(valor);

    if (uri == null || uri.host.isEmpty) return '';

    // Si no se indicó puerto, usa el del backend.
    if (!uri.hasPort && uri.scheme == 'http') {
      return '$valor:8000';
    }

    return valor;
  }

  /// Guarda una dirección nueva. Texto vacío = volver a la automática.
  static Future<void> guardar(String texto) async {
    final prefs = await SharedPreferences.getInstance();
    final valor = normalizar(texto);

    if (valor.isEmpty) {
      _guardada = null;
      await prefs.remove(_clave);
    } else {
      _guardada = valor;
      await prefs.setString(_clave, valor);
    }
  }

  static Future<bool> probar(String texto) async {
    final valor = normalizar(texto);
    final base = valor.isEmpty ? porDefecto : valor;

    try {
      final respuesta = await http
          .get(Uri.parse('$base/salud'))
          .timeout(const Duration(seconds: 6));

      return respuesta.statusCode < 500;
    } catch (_) {
      return false;
    }
  }
}

/// Ventana para ver/cambiar la dirección del servidor.
Future<void> mostrarDialogoServidor(BuildContext context) async {
  final controlador = TextEditingController(text: Servidor.url);
  String? estado;
  bool probando = false;

  await showDialog<void>(
    context: context,
    builder: (ctx) => StatefulBuilder(
      builder: (ctx, setLocal) => AlertDialog(
        title: const Text('Servidor'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Es la dirección de la computadora donde corre el sistema. '
                'Desde el celular usa la IP de la computadora, por ejemplo '
                '192.168.1.50',
                style: TextStyle(fontSize: 13, color: Colors.black54),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: controlador,
                keyboardType: TextInputType.url,
                autocorrect: false,
                decoration: const InputDecoration(
                  labelText: 'Dirección',
                  hintText: 'http://192.168.1.50:8000',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 10),
              if (estado != null)
                Text(
                  estado!,
                  style: TextStyle(
                    color: estado!.startsWith('Conectado')
                        ? Colors.green[800]
                        : Colors.red[700],
                    fontWeight: FontWeight.bold,
                  ),
                ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: probando
                ? null
                : () async {
                    setLocal(() {
                      probando = true;
                      estado = 'Probando conexión...';
                    });

                    final ok = await Servidor.probar(controlador.text);

                    setLocal(() {
                      probando = false;
                      estado = ok
                          ? 'Conectado correctamente'
                          : 'No se pudo conectar. Revisa la dirección y que '
                              'el servidor esté encendido.';
                    });
                  },
            child: const Text('Probar'),
          ),
          TextButton(
            onPressed: () async {
              await Servidor.guardar('');
              if (ctx.mounted) Navigator.pop(ctx);
            },
            child: const Text('Automática'),
          ),
          FilledButton(
            onPressed: () async {
              await Servidor.guardar(controlador.text);
              if (ctx.mounted) Navigator.pop(ctx);
            },
            child: const Text('Guardar'),
          ),
        ],
      ),
    ),
  );

  controlador.dispose();
}

final GlobalKey<NavigatorState> navegadorKey = GlobalKey<NavigatorState>();

// ---------------------------------------------------------------------------
// SESIÓN DEL USUARIO (se conserva al cerrar y abrir la app)
// ---------------------------------------------------------------------------

class Sesion {
  static String? token;
  static String? nombreUsuario;
  static String? nombreCompleto;

  static bool get activa => token != null && token!.isNotEmpty;

  static String get nombreMostrado {
    final completo = nombreCompleto?.trim() ?? '';
    if (completo.isNotEmpty) return completo;
    return nombreUsuario ?? '';
  }

  static Map<String, String> get cabeceras =>
      activa ? {'Authorization': 'Bearer $token'} : <String, String>{};

  static Future<void> cargar() async {
    final prefs = await SharedPreferences.getInstance();
    token = prefs.getString('token');
    nombreUsuario = prefs.getString('nombre_usuario');
    nombreCompleto = prefs.getString('nombre_completo');
  }

  static Future<void> guardar(
    String nuevoToken,
    String usuario,
    String? completo,
  ) async {
    token = nuevoToken;
    nombreUsuario = usuario;
    nombreCompleto = completo;

    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('token', nuevoToken);
    await prefs.setString('nombre_usuario', usuario);

    if (completo == null || completo.isEmpty) {
      await prefs.remove('nombre_completo');
    } else {
      await prefs.setString('nombre_completo', completo);
    }
  }

  static Future<void> cerrar() async {
    final tokenAnterior = token;

    token = null;
    nombreUsuario = null;
    nombreCompleto = null;

    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('token');
    await prefs.remove('nombre_usuario');
    await prefs.remove('nombre_completo');

    // Avisa al servidor (si falla no importa: la sesión local ya se borró).
    if (tokenAnterior != null) {
      try {
        await http
            .post(
              Uri.parse('${Servidor.url}/auth/logout'),
              headers: {'Authorization': 'Bearer $tokenAnterior'},
            )
            .timeout(const Duration(seconds: 5));
      } catch (_) {}
    }
  }
}

// ---------------------------------------------------------------------------
// AYUDANTES PARA HABLAR CON EL SERVIDOR
// ---------------------------------------------------------------------------

class Api {
  static Uri uri(String ruta, [Map<String, String>? parametros]) {
    final base = Uri.parse('${Servidor.url}$ruta');

    if (parametros == null || parametros.isEmpty) return base;

    return base.replace(queryParameters: parametros);
  }

  /// Si el servidor responde 401, la sesión expiró: vuelve al login.
  static Future<void> verificarSesion(int codigo) async {
    if (codigo != 401) return;

    await Sesion.cerrar();

    navegadorKey.currentState?.pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => const LoginPage()),
      (_) => false,
    );

    throw Exception('Tu sesión expiró. Inicia sesión de nuevo.');
  }

  static Future<http.Response> get(
    String ruta, {
    Map<String, String>? parametros,
  }) async {
    final respuesta = await http
        .get(uri(ruta, parametros), headers: Sesion.cabeceras)
        .timeout(const Duration(seconds: 60));

    await verificarSesion(respuesta.statusCode);
    return respuesta;
  }

  static Future<http.Response> patchJson(
    String ruta,
    Map<String, dynamic> cuerpo,
  ) async {
    final respuesta = await http
        .patch(
          uri(ruta),
          headers: {
            ...Sesion.cabeceras,
            'Content-Type': 'application/json',
          },
          body: jsonEncode(cuerpo),
        )
        .timeout(const Duration(seconds: 30));

    await verificarSesion(respuesta.statusCode);
    return respuesta;
  }

  static dynamic jsonDe(http.Response respuesta) {
    return jsonDecode(utf8.decode(respuesta.bodyBytes));
  }

  static String detalleError(http.Response respuesta) {
    final mensajeBase = 'Error del servidor (HTTP ${respuesta.statusCode}).';

    try {
      final datos = jsonDe(respuesta);

      if (datos is Map && datos['detail'] != null) {
        final detalle = datos['detail'];

        if (detalle is String) return detalle;

        if (detalle is List && detalle.isNotEmpty) {
          final primero = detalle.first;
          if (primero is Map && primero['msg'] != null) {
            return primero['msg'].toString();
          }
        }

        return detalle.toString();
      }
    } catch (_) {}

    return mensajeBase;
  }
}

String limpiarError(Object error) {
  return error.toString().replaceFirst('Exception: ', '');
}

String fechaIso(DateTime fecha) {
  return '${fecha.year.toString().padLeft(4, '0')}-'
      '${fecha.month.toString().padLeft(2, '0')}-'
      '${fecha.day.toString().padLeft(2, '0')}';
}

String fechaCorta(DateTime fecha) {
  return '${fecha.day.toString().padLeft(2, '0')}/'
      '${fecha.month.toString().padLeft(2, '0')}/'
      '${fecha.year}';
}

// ---------------------------------------------------------------------------
// FOTOS PENDIENTES (modo sin conexión)
// ---------------------------------------------------------------------------
// Si no hay conexión con el servidor, las fotos se guardan en el navegador
// (IndexedDB) con todo y sus metadatos, y se envían solas cuando vuelve.

/// Error de red: no se pudo ni llegar al servidor.
bool esErrorDeRed(Object error) => error is http.ClientException;

class ResultadoSincronizacion {
  final int enviadas;
  final int fallidas;
  final int restantes;
  final bool sinRed;

  const ResultadoSincronizacion(
    this.enviadas,
    this.fallidas,
    this.restantes,
    this.sinRed,
  );
}

class _FotoPendiente {
  final Object clave;
  final Map<String, dynamic> datos;

  _FotoPendiente(this.clave, this.datos);
}

class Pendientes {
  static const String _almacen = 'fotos';

  /// Cantidad de fotos esperando conexión (la pantalla principal la muestra).
  static final ValueNotifier<int> cantidad = ValueNotifier<int>(0);

  static bool _sincronizando = false;
  static idb.Database? _bd;

  static Future<idb.Database> _abrir() async {
    final existente = _bd;
    if (existente != null) return existente;

    final bd = await idbFactoryBrowser.open(
      'mosca_pinta_pendientes',
      version: 1,
      onUpgradeNeeded: (idb.VersionChangeEvent evento) {
        final idb.Database nueva = evento.database;
        nueva.createObjectStore(_almacen, autoIncrement: true);
      },
    );

    _bd = bd;
    return bd;
  }

  static Future<void> actualizarConteo() async {
    try {
      final bd = await _abrir();
      final txn = bd.transaction(_almacen, idb.idbModeReadOnly);
      final total = await txn.objectStore(_almacen).count();
      await txn.completed;
      cantidad.value = total;
    } catch (_) {
      // Si el navegador no permite IndexedDB, simplemente no hay cola.
    }
  }

  /// Guarda una foto para enviarla después. Devuelve false si no se pudo.
  static Future<bool> agregar({
    required Uint8List bytes,
    required String nombreArchivo,
    required String grupo,
    String nota = '',
    String nombreLote = '',
    int? loteId,
  }) async {
    try {
      final bd = await _abrir();
      final txn = bd.transaction(_almacen, idb.idbModeReadWrite);

      await txn.objectStore(_almacen).add({
        'datos': base64Encode(bytes),
        'nombre': nombreArchivo,
        'nota': nota,
        'lote': nombreLote,
        'grupo': grupo,
        'lote_id': loteId ?? 0,
        'creado': DateTime.now().toIso8601String(),
      });

      await txn.completed;
      await actualizarConteo();
      return true;
    } catch (_) {
      return false;
    }
  }

  static Future<List<_FotoPendiente>> _leerTodas() async {
    final bd = await _abrir();
    final txn = bd.transaction(_almacen, idb.idbModeReadOnly);
    final lista = <_FotoPendiente>[];

    await txn
        .objectStore(_almacen)
        .openCursor(autoAdvance: true)
        .listen((cursor) {
      final valor = cursor.value;

      if (valor is Map) {
        lista.add(
          _FotoPendiente(cursor.key, Map<String, dynamic>.from(valor)),
        );
      }
    }).asFuture();

    await txn.completed;
    return lista;
  }

  static Future<void> _eliminar(Object clave) async {
    final bd = await _abrir();
    final txn = bd.transaction(_almacen, idb.idbModeReadWrite);
    await txn.objectStore(_almacen).delete(clave);
    await txn.completed;
  }

  /// Anota el lote ya creado en las demás fotos del mismo grupo.
  static Future<void> _asignarLote(
    List<_FotoPendiente> fotos,
    int loteId,
  ) async {
    final bd = await _abrir();
    final txn = bd.transaction(_almacen, idb.idbModeReadWrite);
    final almacen = txn.objectStore(_almacen);

    for (final foto in fotos) {
      foto.datos['lote_id'] = loteId;
      await almacen.put(foto.datos, foto.clave);
    }

    await txn.completed;
  }

  /// Intenta enviar todas las fotos pendientes, en orden.
  static Future<ResultadoSincronizacion> sincronizar() async {
    if (_sincronizando || !Sesion.activa) {
      return ResultadoSincronizacion(0, 0, cantidad.value, false);
    }

    _sincronizando = true;

    int enviadas = 0;
    int fallidas = 0;
    bool sinRed = false;

    try {
      final todas = await _leerTodas();
      final lotesPorGrupo = <String, int>{};

      for (int i = 0; i < todas.length; i++) {
        final foto = todas[i];
        final d = foto.datos;
        final grupo = d['grupo']?.toString() ?? '';

        int? loteId = lotesPorGrupo[grupo];
        final guardado = (d['lote_id'] as num?)?.toInt() ?? 0;

        if (loteId == null && guardado > 0) loteId = guardado;

        try {
          final request = http.MultipartRequest(
            'POST',
            Uri.parse('${Servidor.url}/detectar-lote'),
          );

          request.headers.addAll(Sesion.cabeceras);

          final nota = d['nota']?.toString() ?? '';
          if (nota.isNotEmpty) request.fields['nota'] = nota;

          if (loteId != null) {
            request.fields['lote_id'] = loteId.toString();
          } else {
            final nombreLote = d['lote']?.toString() ?? '';
            if (nombreLote.isNotEmpty) {
              request.fields['nombre_lote'] = nombreLote;
            }
          }

          request.files.add(
            http.MultipartFile.fromBytes(
              'imagenes',
              base64Decode(d['datos'].toString()),
              filename: d['nombre']?.toString() ?? 'foto.jpg',
            ),
          );

          final response =
              await request.send().timeout(const Duration(minutes: 3));
          final cuerpo = await response.stream.bytesToString();

          if (response.statusCode == 200) {
            final datos = jsonDecode(cuerpo) as Map;
            final nuevoLote = (datos['lote_id'] as num?)?.toInt();

            if (nuevoLote != null) {
              lotesPorGrupo[grupo] = nuevoLote;

              if (loteId == null) {
                final restantes = todas
                    .sublist(i + 1)
                    .where((x) => x.datos['grupo']?.toString() == grupo)
                    .toList();

                if (restantes.isNotEmpty) {
                  await _asignarLote(restantes, nuevoLote);
                }
              }
            }

            await _eliminar(foto.clave);
            enviadas++;
          } else if (response.statusCode == 401) {
            // Sesión vencida: lleva al login y conserva las fotos.
            await Api.verificarSesion(401);
            break;
          } else if (response.statusCode >= 500) {
            // Falla del servidor: se reintenta más tarde.
            break;
          } else {
            // La foto no se puede analizar (por ejemplo, archivo dañado).
            await _eliminar(foto.clave);
            fallidas++;
          }
        } catch (e) {
          sinRed = esErrorDeRed(e);
          break;
        }
      }
    } catch (_) {
      // Error al leer el almacenamiento: se intentará de nuevo luego.
    } finally {
      _sincronizando = false;
      await actualizarConteo();
    }

    return ResultadoSincronizacion(enviadas, fallidas, cantidad.value, sinRed);
  }
}

// ---------------------------------------------------------------------------
// APLICACIÓN
// ---------------------------------------------------------------------------

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Servidor.cargar();
  await Sesion.cargar();
  runApp(const MoscaPintaApp());
}

class MoscaPintaApp extends StatelessWidget {
  const MoscaPintaApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      navigatorKey: navegadorKey,
      debugShowCheckedModeBanner: false,
      title: 'Mosca Pinta',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.green),
        useMaterial3: true,
        scaffoldBackgroundColor: const Color(0xFFF5F7F5),
      ),
      home: Sesion.activa ? const HomePage() : const LoginPage(),
    );
  }
}

// ---------------------------------------------------------------------------
// REQUISITOS DE LA CONTRASEÑA (los mismos que valida el servidor)
// ---------------------------------------------------------------------------

class RequisitoContrasena {
  final String texto;
  final bool cumple;

  const RequisitoContrasena(this.texto, this.cumple);
}

List<RequisitoContrasena> requisitosContrasena(
  String contrasena,
  String usuario,
) {
  final minuscula = contrasena.toLowerCase();
  final u = usuario.trim().toLowerCase();

  return [
    RequisitoContrasena('Al menos 8 caracteres', contrasena.length >= 8),
    RequisitoContrasena(
      'Una letra mayúscula (A-Z)',
      RegExp(r'[A-ZÁÉÍÓÚÑÜ]').hasMatch(contrasena),
    ),
    RequisitoContrasena(
      'Una letra minúscula (a-z)',
      RegExp(r'[a-záéíóúñü]').hasMatch(contrasena),
    ),
    RequisitoContrasena('Un número (0-9)', RegExp(r'\d').hasMatch(contrasena)),
    RequisitoContrasena(
      'Un símbolo (! @ # \$ % & ...)',
      RegExp(r'[^\p{L}\d\s]', unicode: true).hasMatch(contrasena),
    ),
    RequisitoContrasena('Sin espacios', !contrasena.contains(RegExp(r'\s'))),
    if (u.length >= 3)
      RequisitoContrasena(
        'No contiene tu usuario',
        !minuscula.contains(u),
      ),
  ];
}

bool correoValido(String correo) {
  return RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$').hasMatch(correo.trim());
}

// ---------------------------------------------------------------------------
// INICIO DE SESIÓN / REGISTRO
// ---------------------------------------------------------------------------

class LoginPage extends StatefulWidget {
  const LoginPage({super.key});

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final TextEditingController _usuarioCtrl = TextEditingController();
  final TextEditingController _correoCtrl = TextEditingController();
  final TextEditingController _contrasenaCtrl = TextEditingController();
  final TextEditingController _confirmarCtrl = TextEditingController();
  final TextEditingController _nombreCtrl = TextEditingController();

  bool _modoRegistro = false;
  bool _cargando = false;
  bool _ocultarContrasena = true;

  @override
  void dispose() {
    _usuarioCtrl.dispose();
    _correoCtrl.dispose();
    _contrasenaCtrl.dispose();
    _confirmarCtrl.dispose();
    _nombreCtrl.dispose();
    super.dispose();
  }

  void _mostrarError(String mensaje) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(mensaje)),
    );
  }

  Future<void> _enviar() async {
    if (_cargando) return;

    final usuario = _usuarioCtrl.text.trim();
    final correo = _correoCtrl.text.trim();
    final contrasena = _contrasenaCtrl.text;
    final nombre = _nombreCtrl.text.trim();

    if (_modoRegistro) {
      if (!RegExp(r'^[A-Za-z0-9._-]{3,30}$').hasMatch(usuario)) {
        _mostrarError(
          'El usuario debe tener de 3 a 30 caracteres: letras, números, '
          'punto, guion o guion bajo (sin espacios).',
        );
        return;
      }

      if (!correoValido(correo)) {
        _mostrarError('Escribe un correo electrónico válido.');
        return;
      }

      final faltan = requisitosContrasena(contrasena, usuario)
          .where((r) => !r.cumple)
          .toList();

      if (faltan.isNotEmpty) {
        _mostrarError('La contraseña aún no cumple: ${faltan.first.texto}.');
        return;
      }

      if (contrasena != _confirmarCtrl.text) {
        _mostrarError('Las contraseñas no coinciden.');
        return;
      }
    } else {
      if (usuario.isEmpty || contrasena.isEmpty) {
        _mostrarError('Escribe tu usuario (o correo) y tu contraseña.');
        return;
      }
    }

    setState(() {
      _cargando = true;
    });

    try {
      final ruta = _modoRegistro ? '/auth/registro' : '/auth/login';

      final cuerpo = <String, dynamic>{
        'nombre_usuario': usuario,
        'contrasena': contrasena,
        if (_modoRegistro) 'correo': correo,
        if (_modoRegistro && nombre.isNotEmpty) 'nombre_completo': nombre,
      };

      final respuesta = await http
          .post(
            Uri.parse('${Servidor.url}$ruta'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(cuerpo),
          )
          .timeout(const Duration(seconds: 40));

      if (respuesta.statusCode != 200) {
        if (!mounted) return;
        _mostrarError(Api.detalleError(respuesta));
        return;
      }

      final datos = Api.jsonDe(respuesta);

      if (_modoRegistro) {
        // Falta confirmar el código que se envió al correo.
        if (!mounted) return;

        await Navigator.push(
          context,
          MaterialPageRoute(
            builder: (_) => VerificarCorreoPage(
              correo: datos['correo']?.toString() ?? correo.toLowerCase(),
              correoOculto: datos['correo_oculto']?.toString() ?? correo,
              correoEnviado: datos['correo_enviado'] == true,
            ),
          ),
        );
        return;
      }

      final Map<String, dynamic> usuarioDatos =
          Map<String, dynamic>.from(datos['usuario'] as Map);

      await Sesion.guardar(
        datos['token'].toString(),
        usuarioDatos['nombre_usuario'].toString(),
        usuarioDatos['nombre_completo']?.toString(),
      );

      if (!mounted) return;

      Navigator.pushAndRemoveUntil(
        context,
        MaterialPageRoute(builder: (_) => const HomePage()),
        (_) => false,
      );
    } catch (e) {
      if (!mounted) return;
      _mostrarError(
        'No se pudo conectar con el servidor. Revisa que esté encendido '
        'y la dirección en el botón "Servidor".',
      );
    } finally {
      if (mounted) {
        setState(() {
          _cargando = false;
        });
      }
    }
  }

  Widget _listaRequisitos() {
    return _cajaRequisitos(
      requisitosContrasena(_contrasenaCtrl.text, _usuarioCtrl.text),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        elevation: 0,
        actions: [
          TextButton.icon(
            onPressed: () => mostrarDialogoServidor(context),
            icon: const Icon(Icons.settings_ethernet, size: 18),
            label: const Text('Servidor'),
          ),
        ],
      ),
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Icon(Icons.bug_report, size: 72, color: Colors.green[700]),
                const SizedBox(height: 8),
                const Text(
                  'MOSCA PINTA',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 28, fontWeight: FontWeight.bold),
                ),
                const SizedBox(height: 4),
                const Text(
                  'Detección en cultivos de caña de azúcar',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Colors.black54),
                ),
                const SizedBox(height: 28),
                Card(
                  elevation: 3,
                  child: Padding(
                    padding: const EdgeInsets.all(20),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Text(
                          _modoRegistro ? 'Crear cuenta' : 'Iniciar sesión',
                          textAlign: TextAlign.center,
                          style: const TextStyle(
                            fontSize: 20,
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                        const SizedBox(height: 16),
                        if (_modoRegistro) ...[
                          TextField(
                            controller: _nombreCtrl,
                            textInputAction: TextInputAction.next,
                            textCapitalization: TextCapitalization.words,
                            decoration: const InputDecoration(
                              labelText: 'Nombre completo (opcional)',
                              prefixIcon: Icon(Icons.badge_outlined),
                              border: OutlineInputBorder(),
                            ),
                          ),
                          const SizedBox(height: 12),
                        ],
                        TextField(
                          controller: _usuarioCtrl,
                          textInputAction: TextInputAction.next,
                          autocorrect: false,
                          onChanged: (_) => setState(() {}),
                          decoration: InputDecoration(
                            labelText:
                                _modoRegistro ? 'Usuario' : 'Usuario o correo',
                            prefixIcon: const Icon(Icons.person_outline),
                            border: const OutlineInputBorder(),
                          ),
                        ),
                        const SizedBox(height: 12),
                        if (_modoRegistro) ...[
                          TextField(
                            controller: _correoCtrl,
                            textInputAction: TextInputAction.next,
                            keyboardType: TextInputType.emailAddress,
                            autocorrect: false,
                            decoration: const InputDecoration(
                              labelText: 'Correo electrónico',
                              helperText:
                                  'Te enviaremos un código para confirmarlo',
                              prefixIcon: Icon(Icons.email_outlined),
                              border: OutlineInputBorder(),
                            ),
                          ),
                          const SizedBox(height: 12),
                        ],
                        TextField(
                          controller: _contrasenaCtrl,
                          obscureText: _ocultarContrasena,
                          textInputAction: _modoRegistro
                              ? TextInputAction.next
                              : TextInputAction.done,
                          onChanged: (_) => setState(() {}),
                          onSubmitted: (_) {
                            if (!_modoRegistro) _enviar();
                          },
                          decoration: InputDecoration(
                            labelText: 'Contraseña',
                            prefixIcon: const Icon(Icons.lock_outline),
                            border: const OutlineInputBorder(),
                            suffixIcon: IconButton(
                              onPressed: () {
                                setState(() {
                                  _ocultarContrasena = !_ocultarContrasena;
                                });
                              },
                              icon: Icon(
                                _ocultarContrasena
                                    ? Icons.visibility_outlined
                                    : Icons.visibility_off_outlined,
                              ),
                            ),
                          ),
                        ),
                        if (_modoRegistro) ...[
                          const SizedBox(height: 10),
                          _listaRequisitos(),
                          const SizedBox(height: 12),
                          TextField(
                            controller: _confirmarCtrl,
                            obscureText: _ocultarContrasena,
                            onChanged: (_) => setState(() {}),
                            onSubmitted: (_) => _enviar(),
                            decoration: InputDecoration(
                              labelText: 'Confirmar contraseña',
                              prefixIcon: const Icon(Icons.lock_reset),
                              border: const OutlineInputBorder(),
                              errorText: _confirmarCtrl.text.isNotEmpty &&
                                      _confirmarCtrl.text !=
                                          _contrasenaCtrl.text
                                  ? 'Las contraseñas no coinciden'
                                  : null,
                            ),
                          ),
                        ],
                        const SizedBox(height: 20),
                        ElevatedButton(
                          onPressed: _cargando ? null : _enviar,
                          style: ElevatedButton.styleFrom(
                            backgroundColor: Colors.green[700],
                            foregroundColor: Colors.white,
                            padding: const EdgeInsets.symmetric(vertical: 15),
                          ),
                          child: _cargando
                              ? const SizedBox(
                                  width: 20,
                                  height: 20,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                    color: Colors.white,
                                  ),
                                )
                              : Text(
                                  _modoRegistro
                                      ? 'Enviar código al correo'
                                      : 'Entrar',
                                  style: const TextStyle(fontSize: 16),
                                ),
                        ),
                        if (!_modoRegistro)
                          TextButton(
                            onPressed: _cargando
                                ? null
                                : () {
                                    final texto = _usuarioCtrl.text.trim();

                                    Navigator.push(
                                      context,
                                      MaterialPageRoute(
                                        builder: (_) => RecuperarContrasenaPage(
                                          correoInicial:
                                              texto.contains('@') ? texto : '',
                                        ),
                                      ),
                                    );
                                  },
                            child: const Text('¿Olvidaste tu contraseña?'),
                          ),
                        const SizedBox(height: 8),
                        TextButton(
                          onPressed: _cargando
                              ? null
                              : () {
                                  setState(() {
                                    _modoRegistro = !_modoRegistro;
                                  });
                                },
                          child: Text(
                            _modoRegistro
                                ? 'Ya tengo cuenta: iniciar sesión'
                                : 'No tengo cuenta: registrarme',
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// VERIFICACIÓN DEL CORREO (código de 6 dígitos)
// ---------------------------------------------------------------------------

class VerificarCorreoPage extends StatefulWidget {
  final String correo;
  final String correoOculto;
  final bool correoEnviado;

  const VerificarCorreoPage({
    super.key,
    required this.correo,
    required this.correoOculto,
    required this.correoEnviado,
  });

  @override
  State<VerificarCorreoPage> createState() => _VerificarCorreoPageState();
}

class _VerificarCorreoPageState extends State<VerificarCorreoPage> {
  static const int _espera = 60;

  final TextEditingController _codigoCtrl = TextEditingController();

  Timer? _temporizador;
  int _segundos = _espera;
  bool _verificando = false;
  bool _reenviando = false;
  late bool _correoEnviado;

  @override
  void initState() {
    super.initState();
    _correoEnviado = widget.correoEnviado;
    _iniciarCuenta();
  }

  @override
  void dispose() {
    _temporizador?.cancel();
    _codigoCtrl.dispose();
    super.dispose();
  }

  void _iniciarCuenta() {
    _temporizador?.cancel();
    _segundos = _espera;

    _temporizador = Timer.periodic(const Duration(seconds: 1), (t) {
      if (!mounted) {
        t.cancel();
        return;
      }

      setState(() {
        if (_segundos > 0) _segundos--;
        if (_segundos == 0) t.cancel();
      });
    });
  }

  void _mostrar(String mensaje) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(mensaje)),
    );
  }

  Future<void> _verificar() async {
    if (_verificando) return;

    final codigo = _codigoCtrl.text.trim();

    if (codigo.length != 6) {
      _mostrar('Escribe el código de 6 dígitos.');
      return;
    }

    setState(() {
      _verificando = true;
    });

    try {
      final respuesta = await http
          .post(
            Uri.parse('${Servidor.url}/auth/verificar'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({'correo': widget.correo, 'codigo': codigo}),
          )
          .timeout(const Duration(seconds: 20));

      if (respuesta.statusCode != 200) {
        if (!mounted) return;
        _mostrar(Api.detalleError(respuesta));
        return;
      }

      final datos = Api.jsonDe(respuesta);
      final Map<String, dynamic> usuarioDatos =
          Map<String, dynamic>.from(datos['usuario'] as Map);

      await Sesion.guardar(
        datos['token'].toString(),
        usuarioDatos['nombre_usuario'].toString(),
        usuarioDatos['nombre_completo']?.toString(),
      );

      if (!mounted) return;

      Navigator.pushAndRemoveUntil(
        context,
        MaterialPageRoute(builder: (_) => const HomePage()),
        (_) => false,
      );
    } catch (e) {
      if (!mounted) return;
      _mostrar('No se pudo conectar con el servidor.');
    } finally {
      if (mounted) {
        setState(() {
          _verificando = false;
        });
      }
    }
  }

  Future<void> _reenviar() async {
    if (_reenviando || _segundos > 0) return;

    setState(() {
      _reenviando = true;
    });

    try {
      final respuesta = await http
          .post(
            Uri.parse('${Servidor.url}/auth/reenviar-codigo'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({'correo': widget.correo}),
          )
          .timeout(const Duration(seconds: 40));

      if (!mounted) return;

      if (respuesta.statusCode == 200) {
        final datos = Api.jsonDe(respuesta);

        setState(() {
          _correoEnviado = datos['correo_enviado'] == true;
          _codigoCtrl.clear();
        });
        _iniciarCuenta();
        _mostrar('Te enviamos un código nuevo.');
      } else {
        _mostrar(Api.detalleError(respuesta));
      }
    } catch (e) {
      if (!mounted) return;
      _mostrar('No se pudo conectar con el servidor.');
    } finally {
      if (mounted) {
        setState(() {
          _reenviando = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Verifica tu correo'),
        backgroundColor: Colors.green[700],
        foregroundColor: Colors.white,
      ),
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Icon(
                  Icons.mark_email_read_outlined,
                  size: 72,
                  color: Colors.green[700],
                ),
                const SizedBox(height: 12),
                Text(
                  _correoEnviado
                      ? 'Te enviamos un código de 6 dígitos a\n'
                          '${widget.correoOculto}'
                      : 'Escribe el código de 6 dígitos para\n'
                          '${widget.correoOculto}',
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 16),
                ),
                const SizedBox(height: 4),
                const Text(
                  'Es válido por 10 minutos. Si no lo ves, revisa la '
                  'carpeta de spam.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Colors.black54, fontSize: 13),
                ),
                if (!_correoEnviado) ...[
                  const SizedBox(height: 14),
                  Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: const Color(0xFFFFF8E1),
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(color: const Color(0xFFF9A825)),
                    ),
                    child: const Text(
                      'Modo de pruebas: el servidor no tiene un correo '
                      'configurado, por eso el código aparece en la terminal '
                      'donde corre el backend (uvicorn).',
                      style: TextStyle(fontSize: 13),
                    ),
                  ),
                ],
                const SizedBox(height: 24),
                TextField(
                  controller: _codigoCtrl,
                  autofocus: true,
                  keyboardType: TextInputType.number,
                  textAlign: TextAlign.center,
                  maxLength: 6,
                  inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                  style: const TextStyle(
                    fontSize: 32,
                    letterSpacing: 10,
                    fontWeight: FontWeight.bold,
                  ),
                  onChanged: (valor) {
                    setState(() {});
                    if (valor.length == 6) _verificar();
                  },
                  decoration: const InputDecoration(
                    counterText: '',
                    hintText: '------',
                    border: OutlineInputBorder(),
                    filled: true,
                    fillColor: Colors.white,
                  ),
                ),
                const SizedBox(height: 16),
                ElevatedButton(
                  onPressed: _verificando ? null : _verificar,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.green[700],
                    foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(vertical: 15),
                  ),
                  child: _verificando
                      ? const SizedBox(
                          width: 20,
                          height: 20,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Colors.white,
                          ),
                        )
                      : const Text(
                          'Verificar y crear cuenta',
                          style: TextStyle(fontSize: 16),
                        ),
                ),
                const SizedBox(height: 8),
                TextButton(
                  onPressed: (_segundos > 0 || _reenviando) ? null : _reenviar,
                  child: Text(
                    _segundos > 0
                        ? 'Reenviar código en $_segundos s'
                        : 'Reenviar código',
                  ),
                ),
                TextButton(
                  onPressed: () => Navigator.pop(context),
                  child: const Text('Cambiar correo o datos'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// RECUPERAR CONTRASEÑA OLVIDADA (correo -> código -> contraseña nueva)
// ---------------------------------------------------------------------------

class RecuperarContrasenaPage extends StatefulWidget {
  final String correoInicial;

  const RecuperarContrasenaPage({super.key, this.correoInicial = ''});

  @override
  State<RecuperarContrasenaPage> createState() =>
      _RecuperarContrasenaPageState();
}

class _RecuperarContrasenaPageState extends State<RecuperarContrasenaPage> {
  final TextEditingController _correoCtrl = TextEditingController();
  final TextEditingController _codigoCtrl = TextEditingController();
  final TextEditingController _nuevaCtrl = TextEditingController();
  final TextEditingController _confirmarCtrl = TextEditingController();

  bool _codigoEnviado = false; // paso 2
  bool _correoEnviadoReal = true;
  bool _cargando = false;
  bool _ocultar = true;

  Timer? _temporizador;
  int _segundos = 0;

  @override
  void initState() {
    super.initState();
    _correoCtrl.text = widget.correoInicial;
  }

  @override
  void dispose() {
    _temporizador?.cancel();
    _correoCtrl.dispose();
    _codigoCtrl.dispose();
    _nuevaCtrl.dispose();
    _confirmarCtrl.dispose();
    super.dispose();
  }

  void _mostrar(String mensaje) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(mensaje)),
    );
  }

  void _iniciarCuenta() {
    _temporizador?.cancel();
    _segundos = 60;

    _temporizador = Timer.periodic(const Duration(seconds: 1), (t) {
      if (!mounted) {
        t.cancel();
        return;
      }

      setState(() {
        if (_segundos > 0) _segundos--;
        if (_segundos == 0) t.cancel();
      });
    });
  }

  Future<void> _pedirCodigo() async {
    if (_cargando) return;

    final correo = _correoCtrl.text.trim();

    if (!correoValido(correo)) {
      _mostrar('Escribe el correo con el que te registraste.');
      return;
    }

    setState(() {
      _cargando = true;
    });

    try {
      final respuesta = await http
          .post(
            Uri.parse('${Servidor.url}/auth/olvide-contrasena'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({'correo': correo}),
          )
          .timeout(const Duration(seconds: 40));

      if (!mounted) return;

      if (respuesta.statusCode == 200) {
        final datos = Api.jsonDe(respuesta);

        setState(() {
          _codigoEnviado = true;
          _correoEnviadoReal = datos['correo_enviado'] == true;
        });
        _iniciarCuenta();
      } else {
        _mostrar(Api.detalleError(respuesta));
      }
    } catch (e) {
      if (!mounted) return;
      _mostrar('No se pudo conectar con el servidor.');
    } finally {
      if (mounted) {
        setState(() {
          _cargando = false;
        });
      }
    }
  }

  Future<void> _restablecer() async {
    if (_cargando) return;

    final codigo = _codigoCtrl.text.trim();
    final nueva = _nuevaCtrl.text;

    if (codigo.length != 6) {
      _mostrar('Escribe el código de 6 dígitos.');
      return;
    }

    final faltan = requisitosContrasena(nueva, '')
        .where((r) => !r.cumple)
        .toList();

    if (faltan.isNotEmpty) {
      _mostrar('La contraseña aún no cumple: ${faltan.first.texto}.');
      return;
    }

    if (nueva != _confirmarCtrl.text) {
      _mostrar('Las contraseñas no coinciden.');
      return;
    }

    setState(() {
      _cargando = true;
    });

    try {
      final respuesta = await http
          .post(
            Uri.parse('${Servidor.url}/auth/restablecer-contrasena'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'correo': _correoCtrl.text.trim(),
              'codigo': codigo,
              'contrasena_nueva': nueva,
            }),
          )
          .timeout(const Duration(seconds: 20));

      if (!mounted) return;

      if (respuesta.statusCode == 200) {
        Navigator.pop(context);
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('Contraseña actualizada. Ya puedes iniciar sesión.'),
          ),
        );
      } else {
        _mostrar(Api.detalleError(respuesta));
      }
    } catch (e) {
      if (!mounted) return;
      _mostrar('No se pudo conectar con el servidor.');
    } finally {
      if (mounted) {
        setState(() {
          _cargando = false;
        });
      }
    }
  }

  Widget _paso1() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Icon(Icons.lock_reset, size: 72, color: Colors.green[700]),
        const SizedBox(height: 12),
        const Text(
          'Escribe el correo de tu cuenta y te enviaremos un código para '
          'crear una contraseña nueva.',
          textAlign: TextAlign.center,
          style: TextStyle(fontSize: 15),
        ),
        const SizedBox(height: 20),
        TextField(
          controller: _correoCtrl,
          keyboardType: TextInputType.emailAddress,
          autocorrect: false,
          onSubmitted: (_) => _pedirCodigo(),
          decoration: const InputDecoration(
            labelText: 'Correo electrónico',
            prefixIcon: Icon(Icons.email_outlined),
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 16),
        ElevatedButton(
          onPressed: _cargando ? null : _pedirCodigo,
          style: ElevatedButton.styleFrom(
            backgroundColor: Colors.green[700],
            foregroundColor: Colors.white,
            padding: const EdgeInsets.symmetric(vertical: 15),
          ),
          child: _cargando
              ? const SizedBox(
                  width: 20,
                  height: 20,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: Colors.white,
                  ),
                )
              : const Text('Enviar código', style: TextStyle(fontSize: 16)),
        ),
      ],
    );
  }

  Widget _paso2() {
    final requisitos = requisitosContrasena(_nuevaCtrl.text, '');

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Icon(Icons.mark_email_read_outlined,
            size: 64, color: Colors.green[700]),
        const SizedBox(height: 12),
        Text(
          'Si ${_correoCtrl.text.trim()} está registrado, te enviamos un '
          'código de 6 dígitos. Es válido por 10 minutos.',
          textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 15),
        ),
        if (!_correoEnviadoReal) ...[
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFFFFF8E1),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: const Color(0xFFF9A825)),
            ),
            child: const Text(
              'Modo de pruebas: el código aparece en la terminal donde '
              'corre el backend (uvicorn).',
              style: TextStyle(fontSize: 13),
            ),
          ),
        ],
        const SizedBox(height: 20),
        TextField(
          controller: _codigoCtrl,
          keyboardType: TextInputType.number,
          textAlign: TextAlign.center,
          maxLength: 6,
          inputFormatters: [FilteringTextInputFormatter.digitsOnly],
          style: const TextStyle(
            fontSize: 28,
            letterSpacing: 8,
            fontWeight: FontWeight.bold,
          ),
          decoration: const InputDecoration(
            counterText: '',
            hintText: '------',
            border: OutlineInputBorder(),
            filled: true,
            fillColor: Colors.white,
          ),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _nuevaCtrl,
          obscureText: _ocultar,
          onChanged: (_) => setState(() {}),
          decoration: InputDecoration(
            labelText: 'Contraseña nueva',
            prefixIcon: const Icon(Icons.lock_outline),
            border: const OutlineInputBorder(),
            suffixIcon: IconButton(
              onPressed: () => setState(() => _ocultar = !_ocultar),
              icon: Icon(
                _ocultar
                    ? Icons.visibility_outlined
                    : Icons.visibility_off_outlined,
              ),
            ),
          ),
        ),
        const SizedBox(height: 10),
        _cajaRequisitos(requisitos),
        const SizedBox(height: 12),
        TextField(
          controller: _confirmarCtrl,
          obscureText: _ocultar,
          onChanged: (_) => setState(() {}),
          onSubmitted: (_) => _restablecer(),
          decoration: InputDecoration(
            labelText: 'Confirmar contraseña nueva',
            prefixIcon: const Icon(Icons.lock_reset),
            border: const OutlineInputBorder(),
            errorText: _confirmarCtrl.text.isNotEmpty &&
                    _confirmarCtrl.text != _nuevaCtrl.text
                ? 'Las contraseñas no coinciden'
                : null,
          ),
        ),
        const SizedBox(height: 16),
        ElevatedButton(
          onPressed: _cargando ? null : _restablecer,
          style: ElevatedButton.styleFrom(
            backgroundColor: Colors.green[700],
            foregroundColor: Colors.white,
            padding: const EdgeInsets.symmetric(vertical: 15),
          ),
          child: _cargando
              ? const SizedBox(
                  width: 20,
                  height: 20,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: Colors.white,
                  ),
                )
              : const Text(
                  'Cambiar contraseña',
                  style: TextStyle(fontSize: 16),
                ),
        ),
        const SizedBox(height: 8),
        TextButton(
          onPressed: (_segundos > 0 || _cargando) ? null : _pedirCodigo,
          child: Text(
            _segundos > 0
                ? 'Reenviar código en $_segundos s'
                : 'Reenviar código',
          ),
        ),
        TextButton(
          onPressed: () {
            _temporizador?.cancel();
            setState(() {
              _codigoEnviado = false;
            });
          },
          child: const Text('Cambiar correo'),
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Recuperar contraseña'),
        backgroundColor: Colors.green[700],
        foregroundColor: Colors.white,
      ),
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: _codigoEnviado ? _paso2() : _paso1(),
          ),
        ),
      ),
    );
  }
}

/// Lista de requisitos con palomitas (se reutiliza en varias pantallas).
Widget _cajaRequisitos(List<RequisitoContrasena> requisitos) {
  return Container(
    padding: const EdgeInsets.all(10),
    decoration: BoxDecoration(
      color: const Color(0xFFF1F6F1),
      borderRadius: BorderRadius.circular(8),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Tu contraseña debe tener:',
          style: TextStyle(fontSize: 12.5, fontWeight: FontWeight.bold),
        ),
        const SizedBox(height: 4),
        ...requisitos.map(
          (r) => Padding(
            padding: const EdgeInsets.symmetric(vertical: 1.5),
            child: Row(
              children: [
                Icon(
                  r.cumple ? Icons.check_circle : Icons.circle_outlined,
                  size: 16,
                  color: r.cumple ? Colors.green[700] : Colors.grey,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    r.texto,
                    style: TextStyle(
                      fontSize: 12.5,
                      color: r.cumple ? Colors.green[800] : Colors.black54,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ],
    ),
  );
}

// ---------------------------------------------------------------------------
// CAMBIAR CONTRASEÑA (con la sesión iniciada)
// ---------------------------------------------------------------------------

Future<void> mostrarDialogoCambiarContrasena(BuildContext context) async {
  final actualCtrl = TextEditingController();
  final nuevaCtrl = TextEditingController();
  final confirmarCtrl = TextEditingController();

  bool ocultar = true;
  bool guardando = false;
  String? error;

  final bool? cambiada = await showDialog<bool>(
    context: context,
    barrierDismissible: false,
    builder: (ctx) => StatefulBuilder(
      builder: (ctx, setLocal) {
        Future<void> guardar() async {
          if (guardando) return;

          final faltan = requisitosContrasena(
            nuevaCtrl.text,
            Sesion.nombreUsuario ?? '',
          ).where((r) => !r.cumple).toList();

          if (actualCtrl.text.isEmpty) {
            setLocal(() => error = 'Escribe tu contraseña actual.');
            return;
          }

          if (faltan.isNotEmpty) {
            setLocal(
              () => error = 'La contraseña nueva aún no cumple: '
                  '${faltan.first.texto}.',
            );
            return;
          }

          if (nuevaCtrl.text != confirmarCtrl.text) {
            setLocal(() => error = 'Las contraseñas nuevas no coinciden.');
            return;
          }

          setLocal(() {
            guardando = true;
            error = null;
          });

          try {
            final respuesta = await http
                .post(
                  Uri.parse('${Servidor.url}/auth/cambiar-contrasena'),
                  headers: {
                    ...Sesion.cabeceras,
                    'Content-Type': 'application/json',
                  },
                  body: jsonEncode({
                    'contrasena_actual': actualCtrl.text,
                    'contrasena_nueva': nuevaCtrl.text,
                  }),
                )
                .timeout(const Duration(seconds: 20));

            if (respuesta.statusCode == 200) {
              if (ctx.mounted) Navigator.pop(ctx, true);
              return;
            }

            // 401 aquí significa sesión expirada; el resto son avisos.
            setLocal(() {
              guardando = false;
              error = respuesta.statusCode == 401
                  ? 'Tu sesión expiró. Cierra sesión y entra de nuevo.'
                  : Api.detalleError(respuesta);
            });
          } catch (e) {
            setLocal(() {
              guardando = false;
              error = 'No se pudo conectar con el servidor.';
            });
          }
        }

        return AlertDialog(
          title: const Text('Cambiar contraseña'),
          content: SizedBox(
            width: 380,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  TextField(
                    controller: actualCtrl,
                    obscureText: ocultar,
                    decoration: InputDecoration(
                      labelText: 'Contraseña actual',
                      border: const OutlineInputBorder(),
                      suffixIcon: IconButton(
                        onPressed: () => setLocal(() => ocultar = !ocultar),
                        icon: Icon(
                          ocultar
                              ? Icons.visibility_outlined
                              : Icons.visibility_off_outlined,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: nuevaCtrl,
                    obscureText: ocultar,
                    onChanged: (_) => setLocal(() {}),
                    decoration: const InputDecoration(
                      labelText: 'Contraseña nueva',
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 8),
                  _cajaRequisitos(
                    requisitosContrasena(
                      nuevaCtrl.text,
                      Sesion.nombreUsuario ?? '',
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: confirmarCtrl,
                    obscureText: ocultar,
                    onChanged: (_) => setLocal(() {}),
                    decoration: InputDecoration(
                      labelText: 'Confirmar contraseña nueva',
                      border: const OutlineInputBorder(),
                      errorText: confirmarCtrl.text.isNotEmpty &&
                              confirmarCtrl.text != nuevaCtrl.text
                          ? 'No coinciden'
                          : null,
                    ),
                  ),
                  if (error != null) ...[
                    const SizedBox(height: 10),
                    Text(
                      error!,
                      style: TextStyle(
                        color: Colors.red[700],
                        fontWeight: FontWeight.bold,
                        fontSize: 13,
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: guardando ? null : () => Navigator.pop(ctx, false),
              child: const Text('Cancelar'),
            ),
            FilledButton(
              onPressed: guardando ? null : guardar,
              child: guardando
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Text('Guardar'),
            ),
          ],
        );
      },
    ),
  );

  actualCtrl.dispose();
  nuevaCtrl.dispose();
  confirmarCtrl.dispose();

  if (cambiada == true && context.mounted) {
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Contraseña actualizada.')),
    );
  }
}

// ---------------------------------------------------------------------------
// MODELO AUXILIAR: imagen seleccionada + su vista previa.
// ---------------------------------------------------------------------------

class ImagenSeleccionada {
  final XFile archivo;
  final Uint8List? vistaPrevia;

  ImagenSeleccionada(this.archivo, {this.vistaPrevia});
}

// ---------------------------------------------------------------------------
// EDITOR DE NOTAS (reutilizable)
// ---------------------------------------------------------------------------

class NotaEditor extends StatefulWidget {
  final int registroId;
  final String? notaInicial;
  final void Function(String? nota)? alGuardar;

  const NotaEditor({
    super.key,
    required this.registroId,
    this.notaInicial,
    this.alGuardar,
  });

  @override
  State<NotaEditor> createState() => _NotaEditorState();
}

class _NotaEditorState extends State<NotaEditor> {
  late final TextEditingController _ctrl;
  late String _guardada;
  bool _guardando = false;

  @override
  void initState() {
    super.initState();
    _guardada = widget.notaInicial ?? '';
    _ctrl = TextEditingController(text: _guardada);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  Future<void> _guardar() async {
    final texto = _ctrl.text.trim();

    setState(() {
      _guardando = true;
    });

    try {
      final respuesta = await Api.patchJson(
        '/registros/${widget.registroId}/nota',
        {'nota': texto.isEmpty ? null : texto},
      );

      if (respuesta.statusCode != 200) {
        throw Exception(Api.detalleError(respuesta));
      }

      if (!mounted) return;

      setState(() {
        _guardada = texto;
      });

      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Nota guardada.')),
      );

      widget.alGuardar?.call(texto.isEmpty ? null : texto);
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('No se pudo guardar la nota: ${limpiarError(e)}')),
      );
    } finally {
      if (mounted) {
        setState(() {
          _guardando = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final bool hayCambios = _ctrl.text.trim() != _guardada;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Icon(Icons.sticky_note_2_outlined, color: Colors.green[700]),
            const SizedBox(width: 8),
            const Expanded(
              child: Text(
                'Notas del análisis (opcional)',
                style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        TextField(
          controller: _ctrl,
          maxLines: 4,
          minLines: 2,
          maxLength: 1000,
          onChanged: (_) => setState(() {}),
          decoration: const InputDecoration(
            hintText: 'Ej. Lote norte, hojas con daño visible...',
            border: OutlineInputBorder(),
          ),
        ),
        Align(
          alignment: Alignment.centerRight,
          child: ElevatedButton.icon(
            onPressed: (_guardando || !hayCambios) ? null : _guardar,
            icon: _guardando
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.save),
            label: const Text('Guardar nota'),
            style: ElevatedButton.styleFrom(
              backgroundColor: Colors.green[700],
              foregroundColor: Colors.white,
            ),
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// PANTALLA PRINCIPAL
// ---------------------------------------------------------------------------

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final ImagePicker _picker = ImagePicker();
  final TextEditingController _notaCtrl = TextEditingController();
  final TextEditingController _loteCtrl = TextEditingController();

  final List<ImagenSeleccionada> _imagenes = [];
  List<String> _nombresLote = [];

  bool _analizando = false;
  bool _cargandoVistaPrevia = false;

  // Progreso del análisis (imagen por imagen).
  int _procesadas = 0;
  int _totalAProcesar = 0;

  Timer? _temporizadorEnvio;

  @override
  void initState() {
    super.initState();
    _cargarNombresLote();

    // Fotos guardadas sin conexión: se envían solas cuando vuelve la señal.
    Pendientes.actualizarConteo().then((_) => _intentarEnviarPendientes());
    _temporizadorEnvio = Timer.periodic(
      const Duration(seconds: 15),
      (_) => _intentarEnviarPendientes(),
    );
  }

  Future<void> _intentarEnviarPendientes({bool manual = false}) async {
    if (_analizando) return;

    if (Pendientes.cantidad.value == 0) {
      if (manual && mounted) _mostrarError('No hay fotos pendientes.');
      return;
    }

    final r = await Pendientes.sincronizar();

    if (!mounted) return;

    if (r.enviadas > 0) {
      _mostrarError(
        'Se analizaron ${r.enviadas} foto(s) pendiente(s). '
        'Revísalas en el Historial.'
        '${r.fallidas > 0 ? ' ${r.fallidas} no se pudieron analizar.' : ''}',
      );
      _cargarNombresLote();
    } else if (manual && r.sinRed) {
      _mostrarError('Aún no hay conexión con el servidor.');
    }
  }

  Future<bool> _guardarPendiente(
    ImagenSeleccionada img,
    String grupo,
    int? loteId,
    String nota,
    String nombreLote,
  ) async {
    try {
      final bytes = await img.archivo.readAsBytes();

      return await Pendientes.agregar(
        bytes: bytes,
        nombreArchivo: img.archivo.name,
        grupo: grupo,
        nota: nota,
        nombreLote: nombreLote,
        loteId: loteId,
      );
    } catch (_) {
      return false;
    }
  }

  @override
  void dispose() {
    _temporizadorEnvio?.cancel();
    _notaCtrl.dispose();
    _loteCtrl.dispose();
    super.dispose();
  }

  bool _esHeic(String nombre) {
    final n = nombre.toLowerCase();
    return n.endsWith('.heic') || n.endsWith('.heif');
  }

  /// Nombres de lote usados antes, para elegirlos con un toque.
  Future<void> _cargarNombresLote() async {
    try {
      final respuesta = await Api.get('/lotes');

      if (respuesta.statusCode != 200) return;

      final datos = Api.jsonDe(respuesta);
      final lista = (datos['nombres'] as List?) ?? [];

      if (!mounted) return;

      setState(() {
        _nombresLote = lista.map((e) => e.toString()).toList();
      });
    } catch (_) {
      // No es importante: solo son sugerencias.
    }
  }

  Future<void> _cerrarSesion() async {
    final confirmar = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Cerrar sesión'),
        content: const Text('¿Quieres salir de tu cuenta?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancelar'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Salir'),
          ),
        ],
      ),
    );

    if (confirmar != true) return;

    await Sesion.cerrar();

    if (!mounted) return;

    Navigator.pushAndRemoveUntil(
      context,
      MaterialPageRoute(builder: (_) => const LoginPage()),
      (_) => false,
    );
  }

  Future<void> seleccionarImagenes() async {
    try {
      final List<XFile> archivos = await _picker.pickMultiImage(
        imageQuality: 100,
      );

      if (archivos.isEmpty || !mounted) return;

      await _agregarImagenes(archivos);
    } catch (e) {
      if (!mounted) return;
      _mostrarError('No se pudieron seleccionar las imágenes: $e');
    }
  }

  Future<void> tomarFotografia() async {
    try {
      final XFile? imagen = await _picker.pickImage(
        source: ImageSource.camera,
        imageQuality: 100,
      );

      if (imagen == null || !mounted) return;

      await _agregarImagenes([imagen]);
    } catch (e) {
      if (!mounted) return;
      _mostrarError('No se pudo tomar la fotografía: $e');
    }
  }

  Future<void> _agregarImagenes(List<XFile> archivos) async {
    setState(() {
      _cargandoVistaPrevia = true;
    });

    final nuevas = <ImagenSeleccionada>[];

    for (final archivo in archivos) {
      try {
        final bytes = await _obtenerVistaPrevia(archivo);
        nuevas.add(ImagenSeleccionada(archivo, vistaPrevia: bytes));
      } catch (e) {
        nuevas.add(ImagenSeleccionada(archivo));
        if (mounted) {
          _mostrarError(
            'No se pudo mostrar ${archivo.name}: ${limpiarError(e)}',
          );
        }
      }
    }

    if (!mounted) return;

    setState(() {
      _imagenes.addAll(nuevas);
      _cargandoVistaPrevia = false;
    });
  }

  Future<Uint8List> _obtenerVistaPrevia(XFile imagen) async {
    final Uint8List bytes = await imagen.readAsBytes();

    if (!_esHeic(imagen.name)) return bytes;

    final request = http.MultipartRequest(
      'POST',
      Uri.parse('${Servidor.url}/vista-previa'),
    );

    request.headers.addAll(Sesion.cabeceras);

    request.files.add(
      http.MultipartFile.fromBytes(
        'imagen',
        bytes,
        filename: imagen.name,
      ),
    );

    final response = await request.send();
    final bytesRespuesta = await response.stream.toBytes();

    await Api.verificarSesion(response.statusCode);

    if (response.statusCode != 200) {
      throw Exception(
        'No se pudo convertir la imagen HEIC '
        '(HTTP ${response.statusCode}).',
      );
    }

    return bytesRespuesta;
  }

  void _quitarImagen(int indice) {
    setState(() {
      _imagenes.removeAt(indice);
    });
  }

  void _quitarTodas() {
    setState(() {
      _imagenes.clear();
    });
  }

  /// Envía las imágenes de una en una al mismo lote, para poder mostrar
  /// "Analizando 3 de 8" y que una imagen con problemas no cancele las demás.
  Future<void> analizarImagenes() async {
    if (_imagenes.isEmpty || _analizando) return;

    final lista = List<ImagenSeleccionada>.of(_imagenes);
    final nota = _notaCtrl.text.trim();
    final nombreLote = _loteCtrl.text.trim();

    setState(() {
      _analizando = true;
      _procesadas = 0;
      _totalAProcesar = lista.length;
    });

    int? loteId;
    String? loteNombre;
    Map<String, dynamic> ultimaRespuesta = {};

    final resultados = <Map<String, dynamic>>[];
    final errores = <Map<String, dynamic>>[];
    final imagenesAnalizadas = <ImagenSeleccionada>[];

    // Para las fotos que no se puedan enviar por falta de conexión.
    final grupoLocal = DateTime.now().microsecondsSinceEpoch.toString();
    bool sinRed = false;
    int guardadas = 0;

    try {
      for (int i = 0; i < lista.length; i++) {
        final img = lista[i];

        // Ya sabemos que no hay conexión: se guarda directo.
        if (sinRed) {
          if (await _guardarPendiente(img, grupoLocal, loteId, nota, nombreLote)) {
            guardadas++;
          } else {
            errores.add({
              'nombre_imagen': img.archivo.name,
              'detalle': 'No se pudo guardar para enviarla después.',
            });
          }

          if (!mounted) return;

          setState(() {
            _procesadas = i + 1;
          });
          continue;
        }

        try {
          final request = http.MultipartRequest(
            'POST',
            Uri.parse('${Servidor.url}/detectar-lote'),
          );

          request.headers.addAll(Sesion.cabeceras);

          if (nota.isNotEmpty) request.fields['nota'] = nota;

          if (loteId != null) {
            request.fields['lote_id'] = loteId.toString();
          } else if (nombreLote.isNotEmpty) {
            request.fields['nombre_lote'] = nombreLote;
          }

          final bytes = await img.archivo.readAsBytes();

          request.files.add(
            http.MultipartFile.fromBytes(
              'imagenes',
              bytes,
              filename: img.archivo.name,
            ),
          );

          final response = await request.send().timeout(
                const Duration(minutes: 3),
              );
          final cuerpo = await response.stream.bytesToString();

          await Api.verificarSesion(response.statusCode);

          if (response.statusCode == 200) {
            final datos = Map<String, dynamic>.from(jsonDecode(cuerpo) as Map);

            ultimaRespuesta = datos;
            loteId = (datos['lote_id'] as num?)?.toInt() ?? loteId;
            loteNombre = datos['lote_nombre']?.toString();

            final lis = (datos['resultados'] as List?) ?? [];

            for (final r in lis.whereType<Map>()) {
              final item = Map<String, dynamic>.from(r);
              // Índice de esta imagen dentro de la lista de resultados.
              item['indice'] = imagenesAnalizadas.length;
              resultados.add(item);
              imagenesAnalizadas.add(img);
            }

            final errs = (datos['errores'] as List?) ?? [];

            for (final e in errs.whereType<Map>()) {
              errores.add(Map<String, dynamic>.from(e));
            }
          } else {
            String mensaje = 'Error del servidor (${response.statusCode})';

            try {
              mensaje =
                  (jsonDecode(cuerpo) as Map)['detail']?.toString() ?? mensaje;
            } catch (_) {}

            errores.add({
              'nombre_imagen': img.archivo.name,
              'detalle': mensaje,
            });
          }
        } on Exception catch (e) {
          // Si la sesión expiró, verificarSesion ya llevó al login.
          if (Sesion.activa == false) return;

          if (esErrorDeRed(e)) {
            // Sin conexión con el servidor: se guarda para enviarla luego.
            sinRed = true;

            if (await _guardarPendiente(
              img,
              grupoLocal,
              loteId,
              nota,
              nombreLote,
            )) {
              guardadas++;
            } else {
              errores.add({
                'nombre_imagen': img.archivo.name,
                'detalle': 'Sin conexión y no se pudo guardar para después.',
              });
            }
          } else {
            errores.add({
              'nombre_imagen': img.archivo.name,
              'detalle': limpiarError(e),
            });
          }
        }

        if (!mounted) return;

        setState(() {
          _procesadas = i + 1;
        });
      }

      if (!mounted) return;

      if (resultados.isEmpty && guardadas > 0) {
        setState(() {
          _imagenes.clear();
          _notaCtrl.clear();
          _loteCtrl.clear();
        });
        _mostrarError(
          'Sin conexión: guardé $guardadas foto(s) en tu celular. '
          'Se enviarán y analizarán solas cuando vuelva la conexión.',
        );
        return;
      }

      if (resultados.isEmpty) {
        _mostrarError(
          errores.isEmpty
              ? 'No se pudo analizar ninguna imagen.'
              : 'No se pudo analizar: ${errores.first['detalle']}',
        );
        return;
      }

      final datosFinales = <String, dynamic>{
        ...ultimaRespuesta,
        'lote_id': loteId,
        'lote_nombre': loteNombre,
        'resultados': resultados,
        'errores': errores,
      };

      await Navigator.push(
        context,
        MaterialPageRoute(
          builder: (_) => ResultadosLotePage(
            imagenes: imagenesAnalizadas,
            datos: datosFinales,
          ),
        ),
      );

      if (mounted) {
        setState(() {
          _imagenes.clear();
          _notaCtrl.clear();
          _loteCtrl.clear();
        });
        _cargarNombresLote();

        if (guardadas > 0) {
          _mostrarError(
            '$guardadas foto(s) quedaron guardadas sin conexión y se '
            'enviarán solas cuando vuelva la señal.',
          );
        }
      }
    } catch (e) {
      if (!mounted) return;
      _mostrarError('No se pudo completar el análisis: ${limpiarError(e)}');
    } finally {
      if (mounted) {
        setState(() {
          _analizando = false;
        });
      }
    }
  }

  void _mostrarError(String mensaje) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(mensaje)),
    );
  }

  void _irA(int indice) {
    final Widget? pagina = switch (indice) {
      1 => const HistorialPage(),
      2 => const MapaCalorPage(),
      3 => const ReportePage(),
      _ => null,
    };

    if (pagina == null) return;

    Navigator.push(context, MaterialPageRoute(builder: (_) => pagina));
  }

  Widget _botonGrande({
    required IconData icono,
    required String texto,
    required VoidCallback? alPresionar,
    required bool principal,
  }) {
    final colorBase = Colors.green[700]!;

    return Expanded(
      child: SizedBox(
        height: 110,
        child: ElevatedButton(
          onPressed: alPresionar,
          style: ElevatedButton.styleFrom(
            backgroundColor: principal ? colorBase : Colors.white,
            foregroundColor: principal ? Colors.white : colorBase,
            elevation: principal ? 3 : 1,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(16),
              side: BorderSide(color: colorBase, width: principal ? 0 : 1.5),
            ),
          ),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(icono, size: 38),
              const SizedBox(height: 8),
              Text(
                texto,
                style: const TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.bold,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _construirSeleccion() {
    if (_cargandoVistaPrevia) {
      return const Padding(
        padding: EdgeInsets.all(40),
        child: Center(child: CircularProgressIndicator()),
      );
    }

    if (_imagenes.isEmpty) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 28, horizontal: 16),
        child: Column(
          children: [
            Icon(Icons.add_photo_alternate_outlined,
                size: 54, color: Colors.grey[500]),
            const SizedBox(height: 10),
            Text(
              'Toma o elige las fotos de la caña\n'
              'y aquí aparecerán para analizarlas',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.grey[600], fontSize: 15),
            ),
          ],
        ),
      );
    }

    return Padding(
      padding: const EdgeInsets.all(8),
      child: GridView.builder(
        shrinkWrap: true,
        physics: const NeverScrollableScrollPhysics(),
        itemCount: _imagenes.length,
        gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
          crossAxisCount: 3,
          crossAxisSpacing: 8,
          mainAxisSpacing: 8,
        ),
        itemBuilder: (context, i) {
          final img = _imagenes[i];

          return Stack(
            fit: StackFit.expand,
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: img.vistaPrevia == null
                    ? Container(
                        color: Colors.grey[300],
                        child: const Icon(Icons.broken_image),
                      )
                    : Image.memory(img.vistaPrevia!, fit: BoxFit.cover),
              ),
              Positioned(
                top: 2,
                right: 2,
                child: InkWell(
                  onTap: _analizando ? null : () => _quitarImagen(i),
                  child: const CircleAvatar(
                    radius: 12,
                    backgroundColor: Colors.black54,
                    child: Icon(Icons.close, size: 14, color: Colors.white),
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }

  Widget _construirDatosAnalisis() {
    return Card(
      elevation: 0,
      color: Colors.white,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: Colors.grey.shade300),
      ),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              'Datos del análisis (opcionales)',
              style: TextStyle(fontWeight: FontWeight.bold, fontSize: 15),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _loteCtrl,
              enabled: !_analizando,
              maxLength: 60,
              textCapitalization: TextCapitalization.sentences,
              decoration: const InputDecoration(
                labelText: 'Nombre del lote o parcela',
                hintText: 'Ej. Parcela Norte',
                prefixIcon: Icon(Icons.grass),
                border: OutlineInputBorder(),
                counterText: '',
              ),
            ),
            if (_nombresLote.isNotEmpty) ...[
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 0,
                children: _nombresLote.take(6).map((nombre) {
                  return ActionChip(
                    label: Text(nombre),
                    avatar: const Icon(Icons.history, size: 16),
                    onPressed: _analizando
                        ? null
                        : () {
                            setState(() {
                              _loteCtrl.text = nombre;
                            });
                          },
                  );
                }).toList(),
              ),
            ],
            const SizedBox(height: 12),
            TextField(
              controller: _notaCtrl,
              enabled: !_analizando,
              maxLines: 3,
              minLines: 1,
              maxLength: 1000,
              decoration: const InputDecoration(
                labelText: 'Nota para estas fotos',
                hintText: 'Ej. Hojas con daño visible',
                prefixIcon: Icon(Icons.sticky_note_2_outlined),
                border: OutlineInputBorder(),
              ),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final int total = _imagenes.length;
    final bool ocupado = _cargandoVistaPrevia || _analizando;

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'MOSCA PINTA',
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        centerTitle: true,
        backgroundColor: Colors.green[700],
        foregroundColor: Colors.white,
        actions: [
          IconButton(
            tooltip: 'Servidor',
            onPressed: () => mostrarDialogoServidor(context),
            icon: const Icon(Icons.settings_ethernet),
          ),
          IconButton(
            tooltip: 'Cambiar contraseña',
            onPressed: () => mostrarDialogoCambiarContrasena(context),
            icon: const Icon(Icons.key),
          ),
          IconButton(
            tooltip: 'Cerrar sesión',
            onPressed: _cerrarSesion,
            icon: const Icon(Icons.logout),
          ),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: 0,
        onDestinationSelected: _irA,
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.camera_alt_outlined),
            selectedIcon: Icon(Icons.camera_alt),
            label: 'Analizar',
          ),
          NavigationDestination(
            icon: Icon(Icons.calendar_month_outlined),
            selectedIcon: Icon(Icons.calendar_month),
            label: 'Historial',
          ),
          NavigationDestination(
            icon: Icon(Icons.map_outlined),
            selectedIcon: Icon(Icons.map),
            label: 'Mapa',
          ),
          NavigationDestination(
            icon: Icon(Icons.picture_as_pdf_outlined),
            selectedIcon: Icon(Icons.picture_as_pdf),
            label: 'Reporte',
          ),
        ],
      ),
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 640),
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  'Hola, ${Sesion.nombreMostrado}',
                  style: const TextStyle(
                    fontSize: 22,
                    fontWeight: FontWeight.bold,
                  ),
                ),
                const SizedBox(height: 2),
                const Text(
                  'Paso 1: agrega las fotos de tu cultivo',
                  style: TextStyle(color: Colors.black54, fontSize: 15),
                ),
                const SizedBox(height: 14),
                ValueListenableBuilder<int>(
                  valueListenable: Pendientes.cantidad,
                  builder: (context, pendientes, _) {
                    if (pendientes == 0) return const SizedBox.shrink();

                    return Container(
                      margin: const EdgeInsets.only(bottom: 14),
                      padding: const EdgeInsets.fromLTRB(12, 6, 6, 6),
                      decoration: BoxDecoration(
                        color: const Color(0xFFFFF8E1),
                        borderRadius: BorderRadius.circular(10),
                        border: Border.all(color: const Color(0xFFF9A825)),
                      ),
                      child: Row(
                        children: [
                          const Icon(Icons.cloud_off, color: Color(0xFFF57F17)),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Text(
                              pendientes == 1
                                  ? '1 foto pendiente de enviar'
                                  : '$pendientes fotos pendientes de enviar',
                              style: const TextStyle(fontSize: 14),
                            ),
                          ),
                          TextButton(
                            onPressed: () =>
                                _intentarEnviarPendientes(manual: true),
                            child: const Text('Enviar ahora'),
                          ),
                        ],
                      ),
                    );
                  },
                ),
                Row(
                  children: [
                    _botonGrande(
                      icono: Icons.photo_camera,
                      texto: 'Tomar foto',
                      alPresionar: ocupado ? null : tomarFotografia,
                      principal: true,
                    ),
                    const SizedBox(width: 12),
                    _botonGrande(
                      icono: Icons.photo_library,
                      texto: 'Galería',
                      alPresionar: ocupado ? null : seleccionarImagenes,
                      principal: false,
                    ),
                  ],
                ),
                const SizedBox(height: 16),
                Card(
                  elevation: 0,
                  color: Colors.white,
                  clipBehavior: Clip.antiAlias,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                    side: BorderSide(color: Colors.grey.shade300),
                  ),
                  child: _construirSeleccion(),
                ),
                if (total > 0) ...[
                  const SizedBox(height: 6),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      Text(
                        total == 1
                            ? '1 foto lista'
                            : '$total fotos listas',
                        style: const TextStyle(color: Colors.black54),
                      ),
                      TextButton.icon(
                        onPressed: _analizando ? null : _quitarTodas,
                        icon: const Icon(Icons.delete_outline, size: 18),
                        label: const Text('Quitar todas'),
                      ),
                    ],
                  ),
                  const SizedBox(height: 6),
                  const Text(
                    'Paso 2: revisa los datos y analiza',
                    style: TextStyle(color: Colors.black54, fontSize: 15),
                  ),
                  const SizedBox(height: 10),
                  _construirDatosAnalisis(),
                  const SizedBox(height: 14),
                  if (_analizando) ...[
                    LinearProgressIndicator(
                      value: _totalAProcesar == 0
                          ? null
                          : _procesadas / _totalAProcesar,
                      minHeight: 8,
                      borderRadius: BorderRadius.circular(4),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      'Analizando ${math.min(_procesadas + 1, _totalAProcesar)} '
                      'de $_totalAProcesar...',
                      textAlign: TextAlign.center,
                      style: const TextStyle(color: Colors.black54),
                    ),
                    const SizedBox(height: 8),
                  ],
                  SizedBox(
                    height: 54,
                    child: ElevatedButton.icon(
                      onPressed: ocupado ? null : analizarImagenes,
                      icon: const Icon(Icons.analytics),
                      label: Text(
                        total == 1
                            ? 'Analizar foto'
                            : 'Analizar $total fotos',
                        style: const TextStyle(
                          fontSize: 17,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                      style: ElevatedButton.styleFrom(
                        backgroundColor: Colors.black87,
                        foregroundColor: Colors.white,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(14),
                        ),
                      ),
                    ),
                  ),
                ],
                const SizedBox(height: 20),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// RESULTADOS DEL LOTE (una o varias imágenes)
// ---------------------------------------------------------------------------

class ResultadosLotePage extends StatelessWidget {
  final List<ImagenSeleccionada> imagenes;
  final dynamic datos;

  const ResultadosLotePage({
    super.key,
    required this.imagenes,
    required this.datos,
  });

  String _formatearCaptura(dynamic valor) {
    if (valor == null || valor.toString().isEmpty) {
      return 'Sin fecha de captura';
    }
    return valor.toString().replaceFirst('T', ' ');
  }

  @override
  Widget build(BuildContext context) {
    final Map<String, dynamic> resp =
        datos is Map ? Map<String, dynamic>.from(datos as Map) : {};

    final List<dynamic> resultados =
        resp['resultados'] is List ? List.from(resp['resultados']) : [];

    final List<dynamic> errores =
        resp['errores'] is List ? List.from(resp['errores']) : [];

    final int totalDetecciones =
        int.tryParse('${resp['total_detecciones'] ?? 0}') ?? 0;

    final double confianza =
        (resp['confianza_promedio'] as num?)?.toDouble() ?? 0;

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'RESULTADOS DEL LOTE',
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        centerTitle: true,
        backgroundColor: Colors.green[700],
        foregroundColor: Colors.white,
      ),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          Card(
            elevation: 2,
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                children: [
                  Text(
                    (resp['lote_nombre']?.toString().isNotEmpty ?? false)
                        ? resp['lote_nombre'].toString()
                        : 'Lote #${resp['lote_id'] ?? '?'}',
                    style: const TextStyle(
                      fontSize: 20,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                  const SizedBox(height: 12),
                  Icon(Icons.bug_report, size: 50, color: Colors.green[700]),
                  const SizedBox(height: 6),
                  const Text(
                    'Moscas pintas detectadas en total',
                    style: TextStyle(fontSize: 16),
                  ),
                  Text(
                    '$totalDetecciones',
                    style: const TextStyle(
                      fontSize: 42,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                  const Divider(),
                  Text('Imágenes analizadas: ${resultados.length}'),
                  Text(
                    'Confianza promedio: '
                    '${(confianza * 100).toStringAsFixed(1)} %',
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          const Text(
            'Detalle por imagen',
            style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
          ),
          const Text(
            'Toca una imagen para ver detalles y agregar notas.',
            style: TextStyle(color: Colors.black54, fontSize: 13),
          ),
          const SizedBox(height: 8),
          ...resultados.whereType<Map>().map((item) {
            final int indice = (item['indice'] as num?)?.toInt() ?? -1;
            final ImagenSeleccionada? img =
                (indice >= 0 && indice < imagenes.length)
                    ? imagenes[indice]
                    : null;

            return Card(
              child: ListTile(
                leading: SizedBox(
                  width: 56,
                  height: 56,
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(6),
                    child: img?.vistaPrevia == null
                        ? const Icon(Icons.image)
                        : Image.memory(img!.vistaPrevia!, fit: BoxFit.cover),
                  ),
                ),
                title: Text(
                  item['nombre_imagen']?.toString() ?? 'Imagen',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontWeight: FontWeight.bold),
                ),
                subtitle: Text(
                  'Detecciones: ${item['cantidad'] ?? 0}\n'
                  'Tomada: ${_formatearCaptura(item['fecha_captura'])}',
                ),
                isThreeLine: true,
                trailing: const Icon(Icons.chevron_right),
                onTap: img == null
                    ? null
                    : () {
                        Navigator.push(
                          context,
                          MaterialPageRoute(
                            builder: (_) => ResultadosPage(
                              imagen: img.archivo,
                              imagenVistaPrevia: img.vistaPrevia,
                              datos: Map<String, dynamic>.from(item),
                            ),
                          ),
                        );
                      },
              ),
            );
          }),
          if (errores.isNotEmpty) ...[
            const SizedBox(height: 16),
            const Text(
              'No se pudieron analizar',
              style: TextStyle(
                fontSize: 16,
                fontWeight: FontWeight.bold,
                color: Colors.red,
              ),
            ),
            ...errores.whereType<Map>().map(
                  (e) => Card(
                    child: ListTile(
                      leading: const Icon(
                        Icons.error_outline,
                        color: Colors.red,
                      ),
                      title: Text(e['nombre_imagen']?.toString() ?? 'Imagen'),
                      subtitle: Text(e['detalle']?.toString() ?? ''),
                    ),
                  ),
                ),
          ],
          const SizedBox(height: 20),
          ElevatedButton.icon(
            onPressed: () => Navigator.pop(context),
            icon: const Icon(Icons.add_a_photo),
            label: const Text('Analizar otras imágenes'),
            style: ElevatedButton.styleFrom(
              backgroundColor: Colors.green[700],
              foregroundColor: Colors.white,
              padding: const EdgeInsets.symmetric(vertical: 15),
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// DETALLE DE UNA IMAGEN
// ---------------------------------------------------------------------------

class ResultadosPage extends StatelessWidget {
  final XFile imagen;
  final Uint8List? imagenVistaPrevia;
  final dynamic datos;

  const ResultadosPage({
    super.key,
    required this.imagen,
    required this.imagenVistaPrevia,
    required this.datos,
  });

  @override
  Widget build(BuildContext context) {
    final Map<String, dynamic> respuesta =
        datos is Map ? Map<String, dynamic>.from(datos as Map) : {};

    final int cantidad = int.tryParse('${respuesta['cantidad'] ?? 0}') ?? 0;

    final int? registroId = int.tryParse('${respuesta['registro_id'] ?? ''}');

    final List<dynamic> detecciones = respuesta['detecciones'] is List
        ? List<dynamic>.from(respuesta['detecciones'])
        : [];

    final dynamic metadatosRaw = respuesta['metadatos'];

    final Map<String, dynamic> metadatos = metadatosRaw is Map
        ? Map<String, dynamic>.from(metadatosRaw)
        : <String, dynamic>{};

    final double anchoOriginal =
        (metadatos['ancho_px'] as num?)?.toDouble() ?? 0;

    final double altoOriginal =
        (metadatos['alto_px'] as num?)?.toDouble() ?? 0;

    double sumaConfianzas = 0;

    for (final dynamic item in detecciones) {
      if (item is Map) {
        sumaConfianzas += (item['confianza'] as num?)?.toDouble() ?? 0;
      }
    }

    final double confianzaPromedio =
        detecciones.isEmpty ? 0 : sumaConfianzas / detecciones.length;

    final int confianzaPorcentaje = (confianzaPromedio * 100).round();

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'RESULTADOS',
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        centerTitle: true,
        backgroundColor: Colors.green[700],
        foregroundColor: Colors.white,
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              'Resultado del análisis',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 26, fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 25),
            Card(
              elevation: 3,
              clipBehavior: Clip.antiAlias,
              child: SizedBox(
                height: 300,
                child: imagenVistaPrevia == null
                    ? const Center(
                        child: Text('No se pudo mostrar la fotografía'),
                      )
                    : Stack(
                        fit: StackFit.expand,
                        children: [
                          Image.memory(
                            imagenVistaPrevia!,
                            fit: BoxFit.contain,
                          ),
                          if (anchoOriginal > 0 &&
                              altoOriginal > 0 &&
                              detecciones.isNotEmpty)
                            CustomPaint(
                              painter: DeteccionesPainter(
                                detecciones: detecciones,
                                anchoOriginal: anchoOriginal,
                                altoOriginal: altoOriginal,
                              ),
                            ),
                        ],
                      ),
              ),
            ),
            const SizedBox(height: 10),
            const Text(
              'Los recuadros verdes indican las detecciones del modelo.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.black54, fontSize: 13),
            ),
            const SizedBox(height: 25),
            Card(
              elevation: 2,
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Column(
                  children: [
                    Icon(Icons.bug_report, size: 60, color: Colors.green[700]),
                    const SizedBox(height: 10),
                    const Text(
                      'Moscas pintas detectadas',
                      style: TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                    const SizedBox(height: 5),
                    Text(
                      '$cantidad',
                      style: const TextStyle(
                        fontSize: 42,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 15),
            Card(
              child: ListTile(
                leading: Icon(Icons.verified, color: Colors.green[700]),
                title: const Text(
                  'Confianza promedio',
                  style: TextStyle(fontWeight: FontWeight.bold),
                ),
                subtitle: Text('$confianzaPorcentaje %'),
              ),
            ),
            if (registroId != null) ...[
              const SizedBox(height: 10),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: NotaEditor(
                    registroId: registroId,
                    notaInicial: respuesta['nota']?.toString(),
                  ),
                ),
              ),
            ],
            const SizedBox(height: 10),
            Card(
              child: ExpansionTile(
                leading: Icon(Icons.info_outline, color: Colors.green[700]),
                title: const Text(
                  'Metadatos de la fotografía',
                  style: TextStyle(fontWeight: FontWeight.bold),
                ),
                subtitle: const Text(
                  'Fecha, cámara, dimensiones y ubicación GPS',
                ),
                childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                children: [
                  const Divider(),
                  _filaMetadato('Archivo', imagen.name),
                  _filaMetadato('Fecha original', metadatos['fecha_original']),
                  _filaMetadato(
                    'Fecha de digitalización',
                    metadatos['fecha_digitalizacion'],
                  ),
                  _filaMetadato('Marca', metadatos['marca']),
                  _filaMetadato('Modelo de cámara', metadatos['modelo']),
                  _filaMetadato('Orientación', metadatos['orientacion']),
                  _filaMetadato('Formato', metadatos['formato']),
                  _filaMetadato(
                    'Ancho original',
                    metadatos['ancho_px'] == null
                        ? null
                        : '${metadatos['ancho_px']} px',
                  ),
                  _filaMetadato(
                    'Alto original',
                    metadatos['alto_px'] == null
                        ? null
                        : '${metadatos['alto_px']} px',
                  ),
                  _filaMetadato(
                    'Tamaño del archivo',
                    _formatearTamano(metadatos['tamano_bytes']),
                  ),
                  const Divider(),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      'Ubicación GPS',
                      style: TextStyle(
                        fontWeight: FontWeight.bold,
                        color: Colors.green[800],
                      ),
                    ),
                  ),
                  _filaMetadato('Latitud', metadatos['gps_latitud']),
                  _filaMetadato('Longitud', metadatos['gps_longitud']),
                  if (metadatos.isEmpty)
                    const Padding(
                      padding: EdgeInsets.only(top: 8),
                      child: Text(
                        'El servidor no devolvió metadatos para esta '
                        'fotografía.',
                      ),
                    ),
                ],
              ),
            ),
            const SizedBox(height: 10),
            Card(
              child: ListTile(
                leading: Icon(Icons.smart_toy, color: Colors.green[700]),
                title: const Text(
                  'Modelo utilizado',
                  style: TextStyle(fontWeight: FontWeight.bold),
                ),
                subtitle: const Text('YOLOv8s - modelo entrenado'),
              ),
            ),
            const SizedBox(height: 25),
            ElevatedButton.icon(
              onPressed: () => Navigator.pop(context),
              icon: const Icon(Icons.arrow_back),
              label: const Text('Volver'),
              style: ElevatedButton.styleFrom(
                backgroundColor: Colors.green[700],
                foregroundColor: Colors.white,
                padding: const EdgeInsets.symmetric(vertical: 15),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

Widget _filaMetadato(String etiqueta, dynamic valor) {
  final String texto = valor == null || valor.toString().trim().isEmpty
      ? 'No disponible'
      : valor.toString();

  return Padding(
    padding: const EdgeInsets.symmetric(vertical: 6),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          flex: 4,
          child: Text(
            etiqueta,
            style: const TextStyle(fontWeight: FontWeight.w600),
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          flex: 5,
          child: SelectableText(texto),
        ),
      ],
    ),
  );
}

String _formatearTamano(dynamic valor) {
  if (valor == null) return 'No disponible';

  final double? bytes = double.tryParse(valor.toString());

  if (bytes == null) return valor.toString();

  if (bytes >= 1024 * 1024) {
    return '${(bytes / (1024 * 1024)).toStringAsFixed(2)} MB';
  }

  if (bytes >= 1024) {
    return '${(bytes / 1024).toStringAsFixed(2)} KB';
  }

  return '${bytes.toStringAsFixed(0)} bytes';
}

// ---------------------------------------------------------------------------
// DIBUJO DE RECUADROS DE DETECCIÓN
// ---------------------------------------------------------------------------

class DeteccionesPainter extends CustomPainter {
  final List<dynamic> detecciones;
  final double anchoOriginal;
  final double altoOriginal;

  DeteccionesPainter({
    required this.detecciones,
    required this.anchoOriginal,
    required this.altoOriginal,
  });

  @override
  void paint(Canvas canvas, Size size) {
    if (anchoOriginal <= 0 || altoOriginal <= 0) return;

    final Size tamanoImagen = Size(anchoOriginal, altoOriginal);

    final FittedSizes ajuste = applyBoxFit(
      BoxFit.contain,
      tamanoImagen,
      size,
    );

    final Rect areaImagen = Alignment.center.inscribe(
      ajuste.destination,
      Offset.zero & size,
    );

    final Paint borde = Paint()
      ..color = Colors.greenAccent
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.5;

    final Paint fondoEtiqueta = Paint()..color = Colors.green[800]!;

    final double escalaX = areaImagen.width / anchoOriginal;
    final double escalaY = areaImagen.height / altoOriginal;

    canvas.save();
    canvas.clipRect(areaImagen);

    for (final dynamic item in detecciones) {
      if (item is! Map) continue;

      final double x1 = (item['x1'] as num?)?.toDouble() ?? 0;
      final double y1 = (item['y1'] as num?)?.toDouble() ?? 0;
      final double x2 = (item['x2'] as num?)?.toDouble() ?? 0;
      final double y2 = (item['y2'] as num?)?.toDouble() ?? 0;

      final double confianza = (item['confianza'] as num?)?.toDouble() ?? 0;

      if (x2 <= x1 || y2 <= y1) continue;

      final Rect caja = Rect.fromLTRB(
        areaImagen.left + x1 * escalaX,
        areaImagen.top + y1 * escalaY,
        areaImagen.left + x2 * escalaX,
        areaImagen.top + y2 * escalaY,
      );

      canvas.drawRect(caja, borde);

      final String etiqueta = 'Mosca pinta ${(confianza * 100).round()}%';

      final TextPainter texto = TextPainter(
        text: TextSpan(
          text: etiqueta,
          style: const TextStyle(
            color: Colors.white,
            fontSize: 12,
            fontWeight: FontWeight.bold,
          ),
        ),
        textDirection: TextDirection.ltr,
      );

      texto.layout();

      const double padding = 5;
      final double etiquetaAltura = texto.height + padding * 2;
      final double etiquetaAncho = texto.width + padding * 2;

      final double etiquetaY = caja.top - etiquetaAltura >= areaImagen.top
          ? caja.top - etiquetaAltura
          : caja.top;

      final Rect fondo = Rect.fromLTWH(
        caja.left,
        etiquetaY,
        etiquetaAncho,
        etiquetaAltura,
      );

      canvas.drawRect(fondo, fondoEtiqueta);

      texto.paint(
        canvas,
        Offset(caja.left + padding, etiquetaY + padding),
      );
    }

    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant DeteccionesPainter oldDelegate) {
    return true;
  }
}

// ---------------------------------------------------------------------------
// HISTORIAL: calendario organizado por FECHA DE CAPTURA (metadatos EXIF).
// ---------------------------------------------------------------------------

class HistorialPage extends StatefulWidget {
  const HistorialPage({super.key});

  @override
  State<HistorialPage> createState() => _HistorialPageState();
}

class _HistorialPageState extends State<HistorialPage> {
  DateTime _mesActual = DateTime.now();
  DateTime _diaSeleccionado = DateTime.now();

  List<Map<String, dynamic>> _registros = [];
  bool _cargando = true;
  bool _primeraCarga = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _cargarRegistros();
  }

  Future<void> _cargarRegistros() async {
    if (mounted) {
      setState(() {
        _cargando = true;
        _error = null;
      });
    }

    try {
      final respuesta = await Api.get('/registros');

      if (respuesta.statusCode != 200) {
        throw Exception(Api.detalleError(respuesta));
      }

      final dynamic datos = Api.jsonDe(respuesta);

      if (datos is! Map || datos['registros'] is! List) {
        throw Exception('La respuesta del historial no es válida.');
      }

      final registros = (datos['registros'] as List)
          .whereType<Map>()
          .map((registro) => Map<String, dynamic>.from(registro))
          .toList();

      if (!mounted) return;

      DateTime? masReciente;
      for (final r in registros) {
        final f = _fechaRegistro(r);
        if (f != null && (masReciente == null || f.isAfter(masReciente))) {
          masReciente = f;
        }
      }

      setState(() {
        _registros = registros;
        _cargando = false;

        if (_primeraCarga && masReciente != null) {
          _mesActual = DateTime(masReciente.year, masReciente.month, 1);
          _diaSeleccionado = masReciente;
        }
        _primeraCarga = false;
      });
    } catch (e) {
      if (!mounted) return;

      setState(() {
        _cargando = false;
        _error = 'No se pudo cargar el historial: ${limpiarError(e)}';
      });
    }
  }

  /// Fecha en que se TOMÓ la fotografía (metadatos EXIF).
  DateTime? _fechaRegistro(Map<String, dynamic> registro) {
    final valor = registro['fecha_captura']?.toString();

    if (valor == null || valor.isEmpty) return null;

    try {
      return DateTime.parse(valor);
    } catch (_) {
      return null;
    }
  }

  bool _mismaFecha(DateTime a, DateTime b) {
    return a.year == b.year && a.month == b.month && a.day == b.day;
  }

  List<Map<String, dynamic>> get _registrosDelDia {
    return _registros.where((registro) {
      final fecha = _fechaRegistro(registro);
      return fecha != null && _mismaFecha(fecha, _diaSeleccionado);
    }).toList();
  }

  List<Map<String, dynamic>> get _registrosSinFecha {
    return _registros
        .where((registro) => _fechaRegistro(registro) == null)
        .toList();
  }

  List<Map<String, dynamic>> _registrosDeFecha(DateTime fecha) {
    return _registros.where((registro) {
      final fechaRegistro = _fechaRegistro(registro);
      return fechaRegistro != null && _mismaFecha(fechaRegistro, fecha);
    }).toList();
  }

  String _nombreMes(int mes) {
    const meses = [
      'Enero',
      'Febrero',
      'Marzo',
      'Abril',
      'Mayo',
      'Junio',
      'Julio',
      'Agosto',
      'Septiembre',
      'Octubre',
      'Noviembre',
      'Diciembre',
    ];

    return meses[mes - 1];
  }

  String _formatearHora(DateTime fecha) {
    return '${fecha.hour.toString().padLeft(2, '0')}:'
        '${fecha.minute.toString().padLeft(2, '0')}';
  }

  void _cambiarMes(int diferencia) {
    setState(() {
      _mesActual = DateTime(
        _mesActual.year,
        _mesActual.month + diferencia,
        1,
      );
      _diaSeleccionado = _mesActual;
    });
  }

  Future<void> _editarNota(Map<String, dynamic> registro) async {
    final int? id = int.tryParse('${registro['id']}');

    if (id == null) return;

    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (ctx) => Padding(
        padding: EdgeInsets.fromLTRB(
          16,
          16,
          16,
          16 + MediaQuery.of(ctx).viewInsets.bottom,
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Análisis #$id - ${registro['nombre_imagen'] ?? ''}',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  fontSize: 15,
                  color: Colors.black54,
                ),
              ),
              const SizedBox(height: 12),
              NotaEditor(
                registroId: id,
                notaInicial: registro['nota']?.toString(),
                alGuardar: (nota) {
                  setState(() {
                    registro['nota'] = nota;
                  });
                  Navigator.pop(ctx);
                },
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _tarjetaRegistro(Map<String, dynamic> registro) {
    final fecha = _fechaRegistro(registro);
    final cantidad = registro['cantidad'] ?? 0;
    final confianza =
        (registro['confianza_promedio'] as num?)?.toDouble() ?? 0.0;
    final nombreImagen =
        registro['nombre_imagen']?.toString() ?? 'Imagen sin nombre';
    final id = registro['id']?.toString() ?? '?';
    final loteNombre = registro['lote_nombre']?.toString() ?? '';
    final lote = loteNombre.isNotEmpty
        ? loteNombre
        : (registro['lote_id'] != null ? '#${registro['lote_id']}' : null);
    final nota = registro['nota']?.toString() ?? '';

    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: ListTile(
        leading: CircleAvatar(
          backgroundColor: Colors.green[100],
          child: Icon(Icons.bug_report, color: Colors.green[800]),
        ),
        title: Text(
          'Análisis #$id',
          style: const TextStyle(fontWeight: FontWeight.bold),
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 6),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('Imagen: $nombreImagen'),
              if (lote != null) Text('Lote: $lote'),
              Text('Moscas pintas detectadas: $cantidad'),
              Text(
                'Confianza promedio: '
                '${(confianza * 100).toStringAsFixed(1)} %',
              ),
              if (fecha != null)
                Text('Hora de captura: ${_formatearHora(fecha)}'),
              if (nota.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    'Nota: $nota',
                    style: TextStyle(
                      fontStyle: FontStyle.italic,
                      color: Colors.green[900],
                    ),
                  ),
                ),
            ],
          ),
        ),
        trailing: IconButton(
          tooltip: nota.isEmpty ? 'Agregar nota' : 'Editar nota',
          onPressed: () => _editarNota(registro),
          icon: Icon(
            nota.isEmpty ? Icons.note_add_outlined : Icons.edit_note,
            color: Colors.green[700],
          ),
        ),
        isThreeLine: true,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final primerDiaMes = DateTime(_mesActual.year, _mesActual.month, 1);

    final espaciosIniciales = primerDiaMes.weekday - 1;

    final diasDelMes = DateTime(
      _mesActual.year,
      _mesActual.month + 1,
      0,
    ).day;

    final totalCeldas = espaciosIniciales + diasDelMes;
    final filas = (totalCeldas / 7).ceil();
    final celdas = <Widget>[];

    for (int i = 0; i < filas * 7; i++) {
      final numeroDia = i - espaciosIniciales + 1;

      if (numeroDia < 1 || numeroDia > diasDelMes) {
        celdas.add(const SizedBox.shrink());
        continue;
      }

      final fecha = DateTime(
        _mesActual.year,
        _mesActual.month,
        numeroDia,
      );

      final tieneRegistros = _registrosDeFecha(fecha).isNotEmpty;
      final seleccionado = _mismaFecha(fecha, _diaSeleccionado);
      final hoy = _mismaFecha(fecha, DateTime.now());

      celdas.add(
        InkWell(
          borderRadius: BorderRadius.circular(10),
          onTap: () {
            setState(() {
              _diaSeleccionado = fecha;
            });
          },
          child: Container(
            margin: const EdgeInsets.all(2),
            decoration: BoxDecoration(
              color: seleccionado ? Colors.green[700] : null,
              borderRadius: BorderRadius.circular(10),
              border: hoy && !seleccionado
                  ? Border.all(color: Colors.green, width: 1.5)
                  : null,
            ),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Text(
                  '$numeroDia',
                  style: TextStyle(
                    fontWeight: seleccionado || tieneRegistros
                        ? FontWeight.bold
                        : FontWeight.normal,
                    color: seleccionado ? Colors.white : null,
                  ),
                ),
                const SizedBox(height: 3),
                if (tieneRegistros)
                  Container(
                    width: 6,
                    height: 6,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: seleccionado ? Colors.white : Colors.green[700],
                    ),
                  )
                else
                  const SizedBox(height: 6),
              ],
            ),
          ),
        ),
      );
    }

    final registrosDelDia = _registrosDelDia;
    final registrosSinFecha = _registrosSinFecha;

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'HISTORIAL DE ANÁLISIS',
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        centerTitle: true,
        backgroundColor: Colors.green[700],
        foregroundColor: Colors.white,
        actions: [
          IconButton(
            tooltip: 'Actualizar historial',
            onPressed: _cargando ? null : _cargarRegistros,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _cargarRegistros,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Card(
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(
                  children: [
                    Row(
                      children: [
                        IconButton(
                          tooltip: 'Mes anterior',
                          onPressed: () => _cambiarMes(-1),
                          icon: const Icon(Icons.chevron_left),
                        ),
                        Expanded(
                          child: Text(
                            '${_nombreMes(_mesActual.month)} '
                            '${_mesActual.year}',
                            textAlign: TextAlign.center,
                            style: const TextStyle(
                              fontSize: 19,
                              fontWeight: FontWeight.bold,
                            ),
                          ),
                        ),
                        IconButton(
                          tooltip: 'Mes siguiente',
                          onPressed: () => _cambiarMes(1),
                          icon: const Icon(Icons.chevron_right),
                        ),
                      ],
                    ),
                    const SizedBox(height: 12),
                    Row(
                      children: ['L', 'M', 'X', 'J', 'V', 'S', 'D']
                          .map(
                            (dia) => Expanded(
                              child: Center(
                                child: Text(
                                  dia,
                                  style: const TextStyle(
                                    fontWeight: FontWeight.bold,
                                    color: Colors.black54,
                                  ),
                                ),
                              ),
                            ),
                          )
                          .toList(),
                    ),
                    const SizedBox(height: 8),
                    GridView.count(
                      crossAxisCount: 7,
                      shrinkWrap: true,
                      physics: const NeverScrollableScrollPhysics(),
                      childAspectRatio: 0.9,
                      children: celdas,
                    ),
                    const SizedBox(height: 8),
                    const Row(
                      children: [
                        Icon(Icons.circle, size: 10, color: Colors.green),
                        SizedBox(width: 6),
                        Text(
                          'Día con fotografías analizadas',
                          style: TextStyle(
                            fontSize: 12,
                            color: Colors.black54,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 16),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Fotografías tomadas el ${fechaCorta(_diaSeleccionado)}',
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                    const SizedBox(height: 6),
                    Text(
                      'Registros encontrados: ${registrosDelDia.length}',
                      style: const TextStyle(color: Colors.black54),
                    ),
                  ],
                ),
              ),
            ),
            if (_cargando)
              const Padding(
                padding: EdgeInsets.all(30),
                child: Center(child: CircularProgressIndicator()),
              )
            else if (_error != null)
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    children: [
                      const Icon(
                        Icons.cloud_off,
                        size: 36,
                        color: Colors.red,
                      ),
                      const SizedBox(height: 8),
                      Text(_error!),
                      const SizedBox(height: 8),
                      ElevatedButton.icon(
                        onPressed: _cargarRegistros,
                        icon: const Icon(Icons.refresh),
                        label: const Text('Reintentar'),
                      ),
                    ],
                  ),
                ),
              )
            else if (registrosDelDia.isEmpty)
              const Card(
                child: Padding(
                  padding: EdgeInsets.all(24),
                  child: Column(
                    children: [
                      Icon(
                        Icons.event_available,
                        size: 42,
                        color: Colors.grey,
                      ),
                      SizedBox(height: 8),
                      Text(
                        'No hay fotografías analizadas para este día.',
                        textAlign: TextAlign.center,
                      ),
                    ],
                  ),
                ),
              )
            else
              ...registrosDelDia.map(_tarjetaRegistro),
            if (!_cargando &&
                _error == null &&
                registrosSinFecha.isNotEmpty) ...[
              const SizedBox(height: 16),
              Card(
                color: Colors.amber[50],
                child: ExpansionTile(
                  leading: const Icon(Icons.help_outline),
                  title: Text(
                    'Sin fecha de captura (${registrosSinFecha.length})',
                    style: const TextStyle(fontWeight: FontWeight.bold),
                  ),
                  subtitle: const Text(
                    'Estas imágenes no traen fecha en sus metadatos',
                  ),
                  childrenPadding: const EdgeInsets.all(8),
                  children: registrosSinFecha.map(_tarjetaRegistro).toList(),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// MAPA DE CALOR GEOGRÁFICO (mapa real con zoom)
// ---------------------------------------------------------------------------

class PuntoCalor {
  final int id;
  final LatLng posicion;
  final int cantidad;
  final String nombreImagen;
  final String? fechaCaptura;
  final String? nota;
  final String? lote;

  /// Densidad relativa de 0 a 1 (se calcula al cargar los puntos).
  double densidad = 0;

  PuntoCalor({
    required this.id,
    required this.posicion,
    required this.cantidad,
    required this.nombreImagen,
    this.fechaCaptura,
    this.nota,
    this.lote,
  });
}

/// Azul (baja densidad) -> verde -> amarillo -> naranja -> rojo (alta).
Color colorCalor(double t) {
  const paradas = <Color>[
    Color(0xFF2B6CFF),
    Color(0xFF00C2A8),
    Color(0xFFFFE600),
    Color(0xFFFF8A00),
    Color(0xFFE5231B),
  ];

  final double x = t.clamp(0.0, 1.0).toDouble() * (paradas.length - 1);
  final int i = x.floor().clamp(0, paradas.length - 2).toInt();

  return Color.lerp(paradas[i], paradas[i + 1], x - i)!;
}

/// Calcula la densidad de cada punto sumando las moscas detectadas en los
/// puntos cercanos (más peso cuanto más cerca, con un radio de ~80 m).
void calcularDensidad(List<PuntoCalor> puntos) {
  if (puntos.isEmpty) return;

  const Distance distancia = Distance();
  const double sigma = 80;
  final bool aproximar = puntos.length > 1500;

  final List<double> valores = List<double>.filled(puntos.length, 0);

  for (int i = 0; i < puntos.length; i++) {
    double suma = 0;

    if (aproximar) {
      suma = puntos[i].cantidad.toDouble();
    } else {
      for (int j = 0; j < puntos.length; j++) {
        if (puntos[j].cantidad <= 0) continue;

        final double d = distancia.as(
          LengthUnit.Meter,
          puntos[i].posicion,
          puntos[j].posicion,
        );

        suma += puntos[j].cantidad * math.exp(-(d * d) / (2 * sigma * sigma));
      }
    }

    valores[i] = suma;
  }

  final double maximo = valores.reduce(math.max);

  for (int i = 0; i < puntos.length; i++) {
    puntos[i].densidad = maximo > 0 ? valores[i] / maximo : 0;
  }
}

class MapaCalorPage extends StatefulWidget {
  const MapaCalorPage({super.key});

  @override
  State<MapaCalorPage> createState() => _MapaCalorPageState();
}

class _MapaCalorPageState extends State<MapaCalorPage> {
  MapController _mapCtrl = MapController();

  DateTimeRange? _rango;
  List<PuntoCalor> _puntos = [];
  Map<String, dynamic> _resumen = {};

  bool _cargando = true;
  bool _satelite = false;
  bool _mostrarPuntos = true;
  String? _error;
  int _version = 0;

  @override
  void initState() {
    super.initState();
    _cargar();
  }

  @override
  void dispose() {
    _mapCtrl.dispose();
    super.dispose();
  }

  Map<String, String> get _parametros {
    if (_rango == null) return <String, String>{};

    return {
      'desde': fechaIso(_rango!.start),
      'hasta': fechaIso(_rango!.end),
    };
  }

  Future<void> _cargar() async {
    setState(() {
      _cargando = true;
      _error = null;
    });

    try {
      final respuesta = await Api.get(
        '/mapa-calor/puntos',
        parametros: _parametros,
      );

      if (respuesta.statusCode != 200) {
        throw Exception(Api.detalleError(respuesta));
      }

      final dynamic datos = Api.jsonDe(respuesta);

      if (datos is! Map) {
        throw Exception('La respuesta del mapa no es válida.');
      }

      final List<dynamic> lista =
          datos['puntos'] is List ? datos['puntos'] as List : <dynamic>[];

      final puntos = <PuntoCalor>[];

      for (final item in lista.whereType<Map>()) {
        final double? latitud = (item['latitud'] as num?)?.toDouble();
        final double? longitud = (item['longitud'] as num?)?.toDouble();

        if (latitud == null || longitud == null) continue;

        puntos.add(
          PuntoCalor(
            id: int.tryParse('${item['id']}') ?? 0,
            posicion: LatLng(latitud, longitud),
            cantidad: int.tryParse('${item['cantidad']}') ?? 0,
            nombreImagen: item['nombre_imagen']?.toString() ?? 'Imagen',
            fechaCaptura: item['fecha_captura']?.toString(),
            nota: item['nota']?.toString(),
            lote: (item['lote_nombre']?.toString().isNotEmpty ?? false)
                ? item['lote_nombre'].toString()
                : (item['lote_id'] != null ? '#${item['lote_id']}' : null),
          ),
        );
      }

      calcularDensidad(puntos);

      if (!mounted) return;

      final anterior = _mapCtrl;

      setState(() {
        // Controlador nuevo para que el mapa se ajuste a los nuevos puntos.
        _mapCtrl = MapController();
        _version++;
        _puntos = puntos;
        _resumen = Map<String, dynamic>.from(datos);
        _cargando = false;
      });

      WidgetsBinding.instance.addPostFrameCallback((_) {
        anterior.dispose();
      });
    } catch (e) {
      if (!mounted) return;

      setState(() {
        _cargando = false;
        _error = limpiarError(e);
      });
    }
  }

  Future<void> _elegirRango() async {
    final ahora = DateTime.now();

    final seleccion = await showDateRangePicker(
      context: context,
      firstDate: DateTime(2000),
      lastDate: DateTime(ahora.year + 1, 12, 31),
      initialDateRange: _rango,
      helpText: 'Fecha de captura de las fotografías',
      saveText: 'Aplicar',
    );

    if (seleccion == null || !mounted) return;

    setState(() {
      _rango = seleccion;
    });

    _cargar();
  }

  // ---- Zoom y encuadre ----------------------------------------------------

  CameraFit _encuadre() {
    return CameraFit.bounds(
      bounds: LatLngBounds.fromPoints(
        _puntos.map((p) => p.posicion).toList(),
      ),
      padding: const EdgeInsets.all(56),
      maxZoom: 17,
    );
  }

  MapOptions _opcionesMapa() {
    if (_puntos.length == 1) {
      return MapOptions(
        initialCenter: _puntos.first.posicion,
        initialZoom: 16,
        minZoom: 2,
        maxZoom: 19,
      );
    }

    return MapOptions(
      initialCameraFit: _encuadre(),
      minZoom: 2,
      maxZoom: 19,
    );
  }

  void _zoom(double delta) {
    try {
      final camara = _mapCtrl.camera;

      _mapCtrl.move(
        camara.center,
        (camara.zoom + delta).clamp(2.0, 19.0).toDouble(),
      );
    } catch (_) {}
  }

  void _recentrar() {
    if (_puntos.isEmpty) return;

    try {
      if (_puntos.length == 1) {
        _mapCtrl.move(_puntos.first.posicion, 16);
      } else {
        _mapCtrl.fitCamera(_encuadre());
      }
    } catch (_) {}
  }

  // ---- Capas --------------------------------------------------------------

  List<CircleMarker> _circulosDeCalor() {
    // Los más intensos se dibujan encima.
    final ordenados = [..._puntos]
      ..sort((a, b) => a.densidad.compareTo(b.densidad));

    const radios = <double>[1.0, 0.65, 0.35];
    const alfas = <int>[40, 60, 90];
    const double radioBase = 30; // píxeles

    final circulos = <CircleMarker>[];

    for (final punto in ordenados) {
      if (punto.cantidad <= 0) continue;

      final Color color = colorCalor(punto.densidad);

      for (int i = 0; i < radios.length; i++) {
        circulos.add(
          CircleMarker(
            point: punto.posicion,
            radius: radioBase * radios[i],
            color: color.withAlpha(alfas[i]),
          ),
        );
      }
    }

    return circulos;
  }

  List<Marker> _marcadores() {
    return _puntos.map((punto) {
      return Marker(
        point: punto.posicion,
        width: 24,
        height: 24,
        child: GestureDetector(
          onTap: () => _mostrarDetalle(punto),
          child: Container(
            decoration: BoxDecoration(
              color: Colors.white,
              shape: BoxShape.circle,
              border: Border.all(color: Colors.black87, width: 1.6),
            ),
          ),
        ),
      );
    }).toList();
  }

  void _mostrarDetalle(PuntoCalor punto) {
    final fecha = (punto.fechaCaptura ?? '').replaceFirst('T', ' ');
    final nota = (punto.nota ?? '').trim();

    showModalBottomSheet<void>(
      context: context,
      builder: (ctx) => Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Análisis #${punto.id}',
              style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 8),
            if (punto.lote != null) Text('Lote: ${punto.lote}'),
            Text('Imagen: ${punto.nombreImagen}'),
            Text('Moscas pintas detectadas: ${punto.cantidad}'),
            if (fecha.isNotEmpty) Text('Fecha de captura: $fecha'),
            Text(
              'Ubicación: ${punto.posicion.latitude.toStringAsFixed(5)}, '
              '${punto.posicion.longitude.toStringAsFixed(5)}',
            ),
            if (nota.isNotEmpty) ...[
              const SizedBox(height: 6),
              Text(
                'Nota: $nota',
                style: TextStyle(
                  fontStyle: FontStyle.italic,
                  color: Colors.green[900],
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  // ---- Interfaz -----------------------------------------------------------

  Widget _botonMapa(IconData icono, String ayuda, VoidCallback alPulsar) {
    return FloatingActionButton.small(
      heroTag: null,
      tooltip: ayuda,
      onPressed: alPulsar,
      backgroundColor: Colors.white,
      foregroundColor: Colors.green[800],
      child: Icon(icono),
    );
  }

  Widget _mapa() {
    final int sinGps = int.tryParse('${_resumen['sin_gps'] ?? 0}') ?? 0;

    return Stack(
      children: [
        Positioned.fill(
          child: FlutterMap(
            key: ValueKey(_version),
            mapController: _mapCtrl,
            options: _opcionesMapa(),
            children: [
              TileLayer(
                urlTemplate: _satelite
                    ? 'https://server.arcgisonline.com/ArcGIS/rest/services/'
                        'World_Imagery/MapServer/tile/{z}/{y}/{x}'
                    : 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                userAgentPackageName: 'com.example.mosca_pinta',
                maxNativeZoom: _satelite ? 18 : 19,
              ),
              CircleLayer(circles: _circulosDeCalor()),
              if (_mostrarPuntos) MarkerLayer(markers: _marcadores()),
              RichAttributionWidget(
                attributions: [
                  TextSourceAttribution(
                    _satelite
                        ? 'Esri, Maxar, Earthstar Geographics'
                        : 'OpenStreetMap contributors',
                  ),
                ],
              ),
            ],
          ),
        ),
        Positioned(
          top: 8,
          left: 8,
          child: Card(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    '${_resumen['registros'] ?? 0} análisis',
                    style: const TextStyle(
                      fontSize: 12,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                  Text(
                    '${_resumen['detecciones'] ?? 0} moscas detectadas',
                    style: const TextStyle(fontSize: 12),
                  ),
                  Text(
                    '${_puntos.length} con ubicación GPS',
                    style: const TextStyle(fontSize: 12),
                  ),
                  if (sinGps > 0)
                    Text(
                      '$sinGps sin GPS (no aparecen)',
                      style: TextStyle(
                        fontSize: 11,
                        color: Colors.orange[900],
                      ),
                    ),
                ],
              ),
            ),
          ),
        ),
        Positioned(
          top: 8,
          right: 8,
          child: Column(
            children: [
              _botonMapa(Icons.add, 'Acercar', () => _zoom(1)),
              const SizedBox(height: 6),
              _botonMapa(Icons.remove, 'Alejar', () => _zoom(-1)),
              const SizedBox(height: 6),
              _botonMapa(Icons.center_focus_strong, 'Ver todo', _recentrar),
            ],
          ),
        ),
      ],
    );
  }

  Widget _contenido() {
    if (_cargando) {
      return const Center(child: CircularProgressIndicator());
    }

    if (_error != null) {
      return Center(
        child: Card(
          margin: const EdgeInsets.all(20),
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.cloud_off, size: 36, color: Colors.red),
                const SizedBox(height: 8),
                Text(_error!, textAlign: TextAlign.center),
                const SizedBox(height: 8),
                ElevatedButton.icon(
                  onPressed: _cargar,
                  icon: const Icon(Icons.refresh),
                  label: const Text('Reintentar'),
                ),
              ],
            ),
          ),
        ),
      );
    }

    if (_puntos.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.location_off, size: 48, color: Colors.grey),
              const SizedBox(height: 10),
              const Text(
                'No hay fotografías con ubicación GPS en este periodo.',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 16),
              ),
              const SizedBox(height: 6),
              Text(
                '${_resumen['registros'] ?? 0} análisis encontrados, '
                'ninguno con GPS en sus metadatos.',
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.black54),
              ),
            ],
          ),
        ),
      );
    }

    return _mapa();
  }

  Widget _leyenda() {
    return Row(
      children: [
        const Text('Baja', style: TextStyle(fontSize: 12)),
        const SizedBox(width: 6),
        Expanded(
          child: Container(
            height: 10,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(5),
              gradient: LinearGradient(
                colors: [
                  for (int i = 0; i <= 8; i++) colorCalor(i / 8),
                ],
              ),
            ),
          ),
        ),
        const SizedBox(width: 6),
        const Text('Alta densidad', style: TextStyle(fontSize: 12)),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'MAPA DE CALOR',
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        centerTitle: true,
        backgroundColor: Colors.green[700],
        foregroundColor: Colors.white,
        actions: [
          IconButton(
            tooltip: 'Actualizar',
            onPressed: _cargando ? null : _cargar,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 6),
            child: Row(
              children: [
                Expanded(
                  child: OutlinedButton.icon(
                    onPressed: _elegirRango,
                    icon: const Icon(Icons.date_range),
                    label: Text(
                      _rango == null
                          ? 'Todas las fechas de captura'
                          : '${fechaCorta(_rango!.start)}  -  '
                              '${fechaCorta(_rango!.end)}',
                      overflow: TextOverflow.ellipsis,
                    ),
                    style: OutlinedButton.styleFrom(
                      foregroundColor: Colors.green[700],
                      padding: const EdgeInsets.symmetric(vertical: 12),
                    ),
                  ),
                ),
                if (_rango != null)
                  IconButton(
                    tooltip: 'Quitar filtro de fechas',
                    onPressed: () {
                      setState(() {
                        _rango = null;
                      });
                      _cargar();
                    },
                    icon: const Icon(Icons.close),
                  ),
              ],
            ),
          ),
          Expanded(child: _contenido()),
          Card(
            margin: const EdgeInsets.fromLTRB(12, 6, 12, 6),
            child: Padding(
              padding: const EdgeInsets.all(10),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  _leyenda(),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Expanded(
                        child: SegmentedButton<bool>(
                          segments: const [
                            ButtonSegment<bool>(
                              value: false,
                              label: Text('Calles'),
                              icon: Icon(Icons.map),
                            ),
                            ButtonSegment<bool>(
                              value: true,
                              label: Text('Satélite'),
                              icon: Icon(Icons.satellite_alt),
                            ),
                          ],
                          selected: {_satelite},
                          showSelectedIcon: false,
                          onSelectionChanged: (seleccion) {
                            setState(() {
                              _satelite = seleccion.first;
                            });
                          },
                        ),
                      ),
                      const SizedBox(width: 10),
                      const Text('Puntos', style: TextStyle(fontSize: 12)),
                      Switch(
                        value: _mostrarPuntos,
                        onChanged: (valor) {
                          setState(() {
                            _mostrarPuntos = valor;
                          });
                        },
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 10),
              child: SizedBox(
                width: double.infinity,
                child: ElevatedButton.icon(
                  onPressed: _rango == null
                      ? null
                      : () {
                          Navigator.push(
                            context,
                            MaterialPageRoute(
                              builder: (_) => ReportePage(rangoInicial: _rango),
                            ),
                          );
                        },
                  icon: const Icon(Icons.picture_as_pdf),
                  label: Text(
                    _rango == null
                        ? 'Elige un periodo para generar el PDF'
                        : 'Generar reporte PDF de este periodo',
                  ),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.green[700],
                    foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(vertical: 14),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// REPORTE EN PDF (de una fecha a otra)
// ---------------------------------------------------------------------------

class ReportePage extends StatefulWidget {
  final DateTimeRange? rangoInicial;

  const ReportePage({super.key, this.rangoInicial});

  @override
  State<ReportePage> createState() => _ReportePageState();
}

class _ReportePageState extends State<ReportePage> {
  DateTimeRange? _rango;
  bool _generando = false;

  @override
  void initState() {
    super.initState();
    _rango = widget.rangoInicial;
  }

  void _mensaje(String texto) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(texto)),
    );
  }

  Future<void> _elegirRango() async {
    final ahora = DateTime.now();

    final seleccion = await showDateRangePicker(
      context: context,
      firstDate: DateTime(2000),
      lastDate: DateTime(ahora.year + 1, 12, 31),
      initialDateRange: _rango,
      helpText: 'Fecha de captura de las fotografías',
      saveText: 'Aplicar',
    );

    if (seleccion == null || !mounted) return;

    setState(() {
      _rango = seleccion;
    });
  }

  void _rangoRapido(int dias) {
    final hoy = DateTime.now();
    final fin = DateTime(hoy.year, hoy.month, hoy.day);

    setState(() {
      _rango = DateTimeRange(
        start: fin.subtract(Duration(days: dias - 1)),
        end: fin,
      );
    });
  }

  void _esteMes() {
    final hoy = DateTime.now();

    setState(() {
      _rango = DateTimeRange(
        start: DateTime(hoy.year, hoy.month, 1),
        end: DateTime(hoy.year, hoy.month, hoy.day),
      );
    });
  }

  Future<void> _generar({String parte = 'informe'}) async {
    final rango = _rango;
    final bool evidencias = parte == 'evidencias';

    if (rango == null || _generando) return;

    setState(() {
      _generando = true;
    });

    try {
      final parametros = {
        'desde': fechaIso(rango.start),
        'hasta': fechaIso(rango.end),
      };

      // Primero se verifica que existan análisis en ese periodo.
      final resumen = await Api.get(
        '/mapa-calor/resumen',
        parametros: parametros,
      );

      if (resumen.statusCode != 200) {
        throw Exception(Api.detalleError(resumen));
      }

      final dynamic datos = Api.jsonDe(resumen);
      final int total =
          datos is Map ? (int.tryParse('${datos['registros']}') ?? 0) : 0;

      if (total == 0) {
        if (!mounted) return;
        _mensaje(
          'No hay fotografías con fecha de captura dentro de ese periodo.',
        );
        return;
      }

      // Se genera primero con la sesión en el encabezado. Si el servidor
      // falla, el error real se muestra aquí y la app no se reemplaza.
      final parametrosPdf = {...parametros, 'parte': parte};
      final prueba = await Api.get('/reporte-pdf', parametros: parametrosPdf);

      if (prueba.statusCode != 200) {
        throw Exception(Api.detalleError(prueba));
      }

      // La descarga la hace el navegador, por eso el token va en la dirección.
      final url = Api.uri('/reporte-pdf', {
        ...parametrosPdf,
        'token': Sesion.token ?? '',
      });

      final bool abierto = await launchUrl(
        url,
        mode: LaunchMode.externalApplication,
        webOnlyWindowName: '_self',
      );

      if (!abierto) {
        throw Exception('No se pudo abrir el reporte.');
      }

      if (!mounted) return;
      _mensaje(
        evidencias
            ? 'Evidencias generadas con $total análisis.'
            : 'Reporte generado con $total análisis.',
      );
    } catch (e) {
      if (!mounted) return;
      final String detalle = limpiarError(e);

      _mensaje(
        detalle.startsWith('No se pudo generar el reporte')
            ? detalle
            : 'No se pudo generar el reporte: $detalle',
      );
    } finally {
      if (mounted) {
        setState(() {
          _generando = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'REPORTE EN PDF',
          style: TextStyle(fontWeight: FontWeight.bold),
        ),
        centerTitle: true,
        backgroundColor: Colors.green[700],
        foregroundColor: Colors.white,
      ),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          Icon(Icons.picture_as_pdf, size: 64, color: Colors.green[700]),
          const SizedBox(height: 10),
          const Text(
            'Reporte por periodo',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 24, fontWeight: FontWeight.bold),
          ),
          const SizedBox(height: 6),
          const Text(
            'El reporte incluye resumen, mapa de calor, comportamiento '
            'temporal, recomendaciones y conclusiones. Las fotografías y el '
            'detalle de cada imagen van en el documento de evidencias.',
            textAlign: TextAlign.center,
            style: TextStyle(color: Colors.black54),
          ),
          const SizedBox(height: 24),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Text(
                    'Periodo (fecha de captura de las fotografías)',
                    style: TextStyle(fontWeight: FontWeight.bold),
                  ),
                  const SizedBox(height: 10),
                  OutlinedButton.icon(
                    onPressed: _generando ? null : _elegirRango,
                    icon: const Icon(Icons.date_range),
                    label: Text(
                      _rango == null
                          ? 'Seleccionar fecha inicial y final'
                          : '${fechaCorta(_rango!.start)}  -  '
                              '${fechaCorta(_rango!.end)}',
                    ),
                    style: OutlinedButton.styleFrom(
                      foregroundColor: Colors.green[700],
                      padding: const EdgeInsets.symmetric(vertical: 14),
                    ),
                  ),
                  const SizedBox(height: 10),
                  Wrap(
                    spacing: 8,
                    runSpacing: 4,
                    children: [
                      ActionChip(
                        label: const Text('Últimos 7 días'),
                        onPressed: _generando ? null : () => _rangoRapido(7),
                      ),
                      ActionChip(
                        label: const Text('Últimos 30 días'),
                        onPressed: _generando ? null : () => _rangoRapido(30),
                      ),
                      ActionChip(
                        label: const Text('Este mes'),
                        onPressed: _generando ? null : _esteMes,
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          ElevatedButton.icon(
            onPressed: (_rango == null || _generando) ? null : _generar,
            icon: _generando
                ? const SizedBox(
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: Colors.white,
                    ),
                  )
                : const Icon(Icons.download),
            label: Text(
              _generando ? 'Generando...' : 'Descargar reporte (PDF)',
              style: const TextStyle(fontSize: 16),
            ),
            style: ElevatedButton.styleFrom(
              backgroundColor: Colors.green[700],
              foregroundColor: Colors.white,
              padding: const EdgeInsets.symmetric(vertical: 15),
            ),
          ),
          const SizedBox(height: 10),
          OutlinedButton.icon(
            onPressed: (_rango == null || _generando)
                ? null
                : () => _generar(parte: 'evidencias'),
            icon: const Icon(Icons.photo_library_outlined),
            label: const Text(
              'Descargar evidencias (PDF)',
              style: TextStyle(fontSize: 16),
            ),
            style: OutlinedButton.styleFrom(
              foregroundColor: Colors.green[700],
              padding: const EdgeInsets.symmetric(vertical: 15),
            ),
          ),
          const SizedBox(height: 16),
          const Text(
            'El periodo se filtra por la fecha en que se tomó cada '
            'fotografía. Las imágenes sin fecha de captura no se incluyen.',
            textAlign: TextAlign.center,
            style: TextStyle(color: Colors.black54, fontSize: 13),
          ),
        ],
      ),
    );
  }
}
