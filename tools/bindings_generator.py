#!/usr/bin/env python2

'''
Use CppHeaderParser to parse some C++ headers, and generate binding code for them.

Usage:
        bindings_generator.py BASENAME HEADER1 HEADER2 ... [-- JSON]

  BASENAME is the name used for output files (with added suffixes).
  HEADER1 etc. are the C++ headers to parse

We generate the following:

  * BASENAME.c: C bindings file, with generated C wrapper functions. You will
                need to build this with your project, and make sure it compiles
                properly by adding the proper #includes etc. You can also just
                #include this file itself in one of your existing project files.

  * BASENAME.js: JavaScript bindings file, with generated JavaScript wrapper
                 objects. This is a high-level wrapping, using native JS classes.

  * JSON: An optional JSON object with various optional options:

            ignored: A list of classes and class::methods not to generate code for.
                     Comma separated.
            
            type_processor: Text that is eval()d into a lambda that is run on
                            all arguments. For example, you can use this to
                            change all arguments of type float& to float by
                            "type_processor": "lambda t: t if t != 'float&' else 'float'"

            export: If false, will not export all bindings in the .js file. The
                    default is to export all bindings, which allows
                    you to run something like closure compiler advanced opts on
                    the library+bindings, and the bindings will remain accessible.

          For example, JSON can be { "ignored": "class1,class2::func" }.

The C bindings file is basically a tiny C wrapper around the C++ code.
It's only purpose is to make it easy to access the C++ code in the JS
bindings, and to prevent DFE from removing the code we care about. The
JS bindings do more serious work, creating class structures in JS and
linking them to the C bindings.

Notes:
 * ammo.js is currently the biggest consumer of this code. For some
    more docs you can see that project's README,

      https://github.com/kripken/ammo.js

    Another project using these bindings is box2d.js,

      https://github.com/kripken/box2d.js

 * Functions implemented inline in classes may not be actually
    compiled into bitcode, unless they are actually used. That may
    confuse the bindings generator.

 * C strings (char *) passed to functions are treated in a special way.
    If you pass in a pointer, it is assumed to be a pointer and left as
    is. Otherwise it must be a JS string, and we convert it to a C
    string on the *stack*. The C string will live for the current
    function call. If you need it for longer, you need to create a copy
    in your C++ code.
'''

import os, sys, glob, re

__rootpath__ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def path_from_root(*pathelems):
  return os.path.join(__rootpath__, *pathelems)
sys.path += [path_from_root('')]
from tools.shared import *

# Find ply and CppHeaderParser
sys.path = [path_from_root('third_party', 'ply'), path_from_root('third_party', 'CppHeaderParser')] + sys.path
import CppHeaderParser

#print glob.glob(path_from_root('tests', 'bullet', 'src', 'BulletCollision', 'CollisionDispatch', '*.h'))

basename = sys.argv[1]

ignored = []
type_processor = lambda t: t
export = 1

if '--' in sys.argv:
  index = sys.argv.index('--')
  json = eval(sys.argv[index+1])
  sys.argv = sys.argv[:index]

  if json.get('ignored'):
    ignored = json['ignored'].split(',')
  if json.get('type_processor'):
    type_processor = eval(json['type_processor'])
  if json.get('export'):
    export = json['export']

  print 'zz ignoring', ignored

# First pass - read everything

classes = {}
parents = {}

text = ''
for header in sys.argv[2:]:
  text += '//// ' + header + '\n'
  text += open(header, 'r').read()
all_h_name = basename + '.all.h'
all_h = open(all_h_name, 'w')
all_h.write(text)
all_h.close()

parsed = CppHeaderParser.CppHeader(all_h_name)
print 'zz dir: ', parsed.__dict__.keys()
for classname, clazz in parsed.classes.items() + parsed.structs.items():
  print 'zz see', classname, clazz, type(clazz)
  classes[classname] = clazz
  if type(clazz['methods']) == dict:
    clazz['saved_methods'] = clazz['methods']
    clazz['methods'] = clazz['methods']['public'] # CppHeaderParser doesn't have 'public' etc. in structs. so equalize to that

  if '::' in classname:
    assert classname.count('::') == 1
    parents[classname.split('::')[1]] = classname.split('::')[0]

  if hasattr(clazz, '_public_structs'): # This is a class
    for sname, struct in clazz._public_structs.iteritems():
      parents[sname] = classname
      classes[classname + '::' + sname] = struct
      struct['name'] = sname # Missing in CppHeaderParser
      print 'zz seen struct %s in %s' % (sname, classname)

  if 'fields' in clazz: # This is a struct
    print 'zz add properties!'
    clazz['properties'] = { 'public': clazz['fields'] }
    clazz['name'] = classname
    clazz['inherits'] = []
print 'zz parents: ', parents

def check_has_constructor(clazz):
  for method in clazz['methods']:
    if method['constructor'] and not method['destructor']: return True
  return False

for classname, clazz in parsed.classes.items() + parsed.structs.items():
  # Various precalculations
  print 'zz precalc', classname
  for method in clazz['methods'][:]:
    method['constructor'] = method['constructor'] or (method['name'] == classname) # work around cppheaderparser issue
    print 'z constructorhmm?', method['name'], method['constructor']#, constructor, method['name'], classname
    args = method['parameters']

    #if method['name'] == 'addWheel': print 'qqqq', classname, method

    # Fill in some missing stuff
    for i in range(len(args)):
      if args[i]['pointer'] and '*' not in args[i]['type']:
        args[i]['type'] += '*'
      if args[i]['reference'] and '&' not in args[i]['type']:
        args[i]['type'] += '&'
      args[i]['type'] = type_processor(args[i]['type'])
      #raw = args[i]['type'].replace('&', '').replace('*', '')
      #if raw in classes:

    default_param = len(args)+1
    for i in range(len(args)):
      if args[i].get('default'):
        default_param = i+1
        break

    method['parameters'] = [args[:i] for i in range(default_param-1, len(args)+1)]
    print 'zz ', classname, 'has parameters in range', range(default_param-1, len(args)+1)

    method['returns_text'] = method['returns']
    if method['static']:
      method['returns_text'] = method['returns_text'].replace('static', '')

    # Implement operators
    if '__operator__' in method['name']:
      if 'assignment' in method['name']:
        method['name'] = 'op_set'
        method['operator'] = '  return *self = arg0;'
      elif 'add' in method['name']:
        method['name'] = 'op_add'
        method['operator'] = '  return *self += arg0;'
      elif 'sub' in method['name']:
        print 'zz subsubsub ', classname, method['name'], method['parameters'][0]
        method['name'] = 'op_sub'
        if len(method['parameters'][0]) == 0:
          method['operator'] = '  static %s ret; ret = -*self; return ret;' % method['returns']
        else:
          method['operator'] = '  return *self -= arg0; // %d : %s' % (len(method['parameters'][0]), method['parameters'][0][0]['name'])
      elif 'imul' in method['name']:
        method['name'] = 'op_mul'
        method['operator'] = '  return *self *= arg0;'
      elif 'mul' in method['name']:
        method['name'] = 'op_mul'
        method['operator'] = '  static %s ret; ret = *self * arg0; return ret;' % method['returns']
      elif 'div' in method['name']:
        method['name'] = 'op_div'
        method['operator'] = '  return *self /= arg0;'
      elif 'getitem' in method['name']:
        method['name'] = 'op_get'
        method['operator'] = '  return (*self)[arg0];'
      elif 'delete' in method['name']:
        method['ignore'] = True
      elif 'new' in method['name']:
        method['ignore'] = True
      elif 'eq' in method['name']:
        method['name'] = 'op_comp'
        method['operator'] = '  return arg0 == arg1;' if len(method['parameters'][0]) == 2 else '  return *self == arg0;'
      else:
        print 'zz unknown operator:', method['name'], ', ignoring'
        method['ignore'] = True

    # Fill in some missing stuff
    method['returns_text'] = method['returns_text'].replace('&', '').replace('*', '')
    if method['returns_text'] in parents:
      method['returns_text'] = parents[method['returns_text']] + '::' + method['returns_text']
    if method.get('returns_const'): method['returns_text'] = 'const ' + method['returns_text']
    if method.get('returns_pointer'):
      while method['returns_text'].count('*') < method['returns_pointer']:
        method['returns_text'] += '*'
    if method.get('returns_reference'): method['returns_text'] += '&'
    method['returns_text'] = type_processor(method['returns_text'])

    #print 'zz %s::%s gets %s and returns %s' % (classname, method['name'], str([arg['type'] for arg in method['parameters']]), method['returns_text'])

    # Add getters/setters for public members
    for prop in clazz['properties']['public']:
      if classname + '::' + prop['name'] in ignored: continue
      if prop.get('array_dimensions'):
        print 'zz warning: ignoring getter/setter for array', classname + '::' + prop['name']
        continue
      type_ = prop['type'].replace('mutable ', '')#.replace(' ', '')
      if '<' in prop['name'] or '<' in type_:
        print 'zz warning: ignoring getter/setter for templated class', classname + '::' + prop['name']
        continue
      reference = type_ in classes # a raw struct or class as a prop means we need to work with a ref
      clazz['methods'].append({
        'getter': True,
        'name': 'get_' + prop['name'],
        'constructor': False,
        'destructor': False,
        'static': False,
        'returns': type_.replace(' *', '').replace('*', ''),
        'returns_text': type_ + ('&' if reference else ''),
        'returns_reference': reference,
        'returns_pointer': '*' in type_,
        'pure_virtual': False,
        'parameters': [[]],
      })
      clazz['methods'].append({
        'setter': True,
        'name': 'set_' + prop['name'],
        'constructor': False,
        'destructor': False,
        'static': False,
        'returns': 'void',
        'returns_text': 'void',
        'returns_reference': False,
        'returns_pointer': False,
        'pure_virtual': False,
        'parameters': [[{
          'type': type_ + ('&' if reference else ''),
          'name': 'value',
        }]],
      })

  print 'zz is effectively abstract?', clazz['name'], classname, '0'
  if 'saved_methods' in clazz and not check_has_constructor(clazz):
    print 'zz is effectively abstract?', clazz['name'], '1'
    # Having a private constructor and no public constructor means you are, in effect, abstract
    for private_method in clazz['saved_methods']['private']:
      print 'zz is effectively abstract?', clazz['name'], '2'
      if private_method['constructor']:
        print 'zz is effectively abstract?', clazz['name'], '3'
        clazz['effectively_abstract'] = True

  # Add destroyer
  if not clazz.get('abstract') and not clazz.get('effectively_abstract'):
    clazz['methods'].append({
      'destroyer': True,
      'name': '__destroy__',
      'constructor': False,
      'destructor': False,
      'static': False,
      'returns': 'void',
      'returns_text': 'void',
      'returns_reference': False,
      'returns_pointer': False,
      'pure_virtual': False,
      'parameters': [[]],
    })

  clazz['methods'] = filter(lambda method: not method.get('ignore'), clazz['methods'])

# Explore all functions we need to generate, including parent classes, handling of overloading, etc.

def clean_type(t):
  return t.replace('const ', '').replace('struct ', '').replace('&', '').replace('*', '').replace(' ', '')

def fix_template_value(t): # Not sure why this is needed, might be a bug in CppHeaderParser
  if t == 'unsignedshortint':
    return 'unsigned short int'
  elif t == 'unsignedint':
    return 'unsigned int'
  return t

def copy_args(args):
  ret = []
  for arg in args:
    copiedarg = {
      'type': arg['type'],
      'name': arg['name'],
    }
    ret.append(copiedarg)
  return ret

for classname, clazz in parsed.classes.items() + parsed.structs.items():
  clazz['final_methods'] = {}

  def explore(subclass, template_name=None, template_value=None):
    # Do our functions first, and do not let later classes override
    for method in subclass['methods']:
      print classname, 'exploring', subclass['name'], '::', method['name']

      if method['constructor']:
        if clazz != subclass:
          print "zz Subclasses cannot directly use their parent's constructors"
          continue
      if method['destructor']:
        print 'zz Nothing to do there'
        continue

      if method.get('operator') and subclass is not clazz:
        print 'zz Do not use parent class operators. Cast to that class if you need those operators (castObject)'
        continue

      if method['name'] not in clazz['final_methods']:
        copied = clazz['final_methods'][method['name']] = {}
        for key in ['name', 'constructor', 'static', 'returns', 'returns_text', 'returns_reference', 'returns_pointer', 'destructor', 'pure_virtual',
                    'getter', 'setter', 'destroyer', 'operator']:
          copied[key] = method.get(key)
        copied['origin'] = subclass
        copied['parameters'] = [];
        for args in method['parameters']:
          # Copy the arguments, since templating may cause them to be altered
          copied['parameters'].append(copy_args(args))
        if template_name:
          # Set template values
          copied['returns'] = copied['returns'].replace(template_name, template_value)
          copied['returns_text'] = copied['returns_text'].replace(template_name, template_value)
          for args in copied['parameters']:
            for arg in args:
              arg['type'] = arg['type'].replace(template_name, template_value)
      else:
        # Merge the new function in the best way we can. Two signatures (args) must differ in their number

        if method.get('operator'): continue # do not merge operators

        curr = clazz['final_methods'][method['name']]

        if curr['origin'] is not subclass: continue # child class functions mask/hide parent functions of the same name in C++

        problem = False
        for curr_args in curr['parameters']:
          for method_args in method['parameters']:
            if len(curr_args) == len(method_args):
              problem = True
        if problem:
          print 'Warning: Cannot mix in overloaded functions', method['name'], 'in class', classname, ', skipping'
          continue
        # TODO: Other compatibility checks, if any?

        curr['parameters'] += map(copy_args, method['parameters'])

        print 'zz ', classname, 'has updated parameters of ', curr['parameters']

    # Recurse
    if subclass.get('inherits'):
      for parent in subclass['inherits']:
        parent = parent['class']
        template_name = None
        template_value = None
        if '<' in parent:
          parent, template = parent.split('<')
          template_name = classes[parent]['template_typename']
          template_value = fix_template_value(template.replace('>', ''))
          print 'template', template_value, 'for', classname, '::', parent, ' | ', template_name
        if parent not in classes and '::' in classname: # They might both be subclasses in the same parent
          parent = classname.split('::')[0] + '::' + parent
        if parent not in classes:
          print 'Warning: parent class', parent, 'not a known class. Ignoring.'
          return
        explore(classes[parent], template_name, template_value)

  explore(clazz)

  for method in clazz['final_methods'].itervalues():
    method['parameters'].sort(key=len)

# Second pass - generate bindings
# TODO: Bind virtual functions using dynamic binding in the C binding code

funcs = {} # name -> # of copies in the original, and originalname in a copy

gen_c = open(basename + '.cpp', 'w')
gen_js = open(basename + '.js', 'w')

gen_c.write('extern "C" {\n')

gen_js.write('''
// Bindings utilities

var Object__cache = {}; // we do it this way so we do not modify |Object|
function wrapPointer(ptr, __class__) {
  var cache = __class__ ? __class__.prototype.__cache__ : Object__cache;
  var ret = cache[ptr];
  if (ret) return ret;
  __class__ = __class__ || Object;
  ret = Object.create(__class__.prototype);
  ret.ptr = ptr;
  ret.__class__ = __class__;
  return cache[ptr] = ret;
}
Module['wrapPointer'] = wrapPointer;

function castObject(obj, __class__) {
  return wrapPointer(obj.ptr, __class__);
}
Module['castObject'] = castObject;

Module['NULL'] = wrapPointer(0);

function destroy(obj) {
  if (!obj['__destroy__']) throw 'Error: Cannot destroy object. (Did you create it yourself?)';
  obj['__destroy__']();
  // Remove from cache, so the object can be GC'd and refs added onto it released
  if (obj.__class__ !== Object) {
    delete obj.__class__.prototype.__cache__[obj.ptr];
  } else {
    delete Object__cache[obj.ptr];
  }
}
Module['destroy'] = destroy;

function compare(obj1, obj2) {
  return obj1.ptr === obj2.ptr;
}
Module['compare'] = compare;

function getPointer(obj) {
  return obj.ptr;
}
Module['getPointer'] = getPointer;

function getClass(obj) {
  return obj.__class__;
}
Module['getClass'] = getClass;

function customizeVTable(object, replacementPairs) {
  // Does not handle multiple inheritance

  // Find out vtable size
  var vTable = getValue(object.ptr, 'void*');
  // This assumes our modification where we null-terminate vtables
  var size = 0;
  while (getValue(vTable + Runtime.QUANTUM_SIZE*size, 'void*')) {
    size++;
  }

  // Prepare replacement lookup table and add replacements to FUNCTION_TABLE
  // There is actually no good way to do this! So we do the following hack:
  // We create a fake vtable with canary functions, to detect which actual
  // function is being called
  var vTable2 = _malloc(size*Runtime.QUANTUM_SIZE);
  setValue(object.ptr, vTable2, 'void*');
  var canaryValue;
  var functions = FUNCTION_TABLE.length;
  for (var i = 0; i < size; i++) {
    var index = FUNCTION_TABLE.length;
    (function(j) {
      FUNCTION_TABLE.push(function() {
        canaryValue = j;
      });
    })(i);
    FUNCTION_TABLE.push(0);
    setValue(vTable2 + Runtime.QUANTUM_SIZE*i, index, 'void*');
  }
  var args = [{ptr: 0}];
  replacementPairs.forEach(function(pair) {
    // We need the wrapper function that converts arguments to not fail. Keep adding arguments til it works.
    while(1) {
      try {
        pair['original'].apply(object, args);
        break;
      } catch(e) {
        args.push(args[0]);
      }
    }
    pair.originalIndex = getValue(vTable + canaryValue*Runtime.QUANTUM_SIZE, 'void*');
  });
  FUNCTION_TABLE = FUNCTION_TABLE.slice(0, functions);

  // Do the replacements

  var replacements = {};
  replacementPairs.forEach(function(pair) {
    var replacementIndex = FUNCTION_TABLE.length;
    FUNCTION_TABLE.push(pair['replacement']);
    FUNCTION_TABLE.push(0);
    replacements[pair.originalIndex] = replacementIndex;
  });

  // Copy and modify vtable
  for (var i = 0; i < size; i++) {
    var value = getValue(vTable + Runtime.QUANTUM_SIZE*i, 'void*');
    if (value in replacements) value = replacements[value];
    setValue(vTable2 + Runtime.QUANTUM_SIZE*i, value, 'void*');
  }
  return object;
}
Module['customizeVTable'] = customizeVTable;

// Converts a value into a C-style string.
function ensureString(value) {
  if (typeof value == 'number') return value;
  return allocate(intArrayFromString(value), 'i8', ALLOC_STACK);
}
''')

def generate_wrapping_code(classname):
  return '''%(classname)s.prototype.__cache__ = {};
''' % { 'classname': classname }
# %(classname)s.prototype['fields'] = Runtime.generateStructInfo(null, '%(classname)s'); - consider adding this

def generate_class(generating_classname, classname, clazz): # TODO: deprecate generating?
  print 'zz generating:', generating_classname, classname
  generating_classname_head = generating_classname.split('::')[-1]
  classname_head = classname.split('::')[-1]

  inherited = generating_classname_head != classname_head

  abstract = clazz['abstract']
  if abstract:
    # For abstract base classes, add a function definition on top. There is no constructor
    gen_js.write('\nfunction ' + generating_classname_head + ('(){ throw "%s is abstract!" }\n' % generating_classname_head) + generate_wrapping_code(generating_classname_head))
    if export:
      gen_js.write('''Module['%s'] = %s;
''' % (generating_classname_head, generating_classname_head))

  print 'zz methods: ', clazz['final_methods'].keys()
  for method in clazz['final_methods'].itervalues():
    mname = method['name']
    if classname_head + '::' + mname in ignored:
      print 'zz ignoring', mname
      continue

    params = method['parameters']
    constructor = method['constructor']
    destructor = method['destructor']
    static = method['static']

    print 'zz generating %s::%s' % (generating_classname, method['name'])

    if destructor: continue
    if constructor and inherited: continue
    if constructor and clazz['abstract']: continue # do not generate constructors for abstract base classes

    for args in params:
      for i in range(len(args)):
        #print 'zz   arggggggg', classname, 'x', mname, 'x', args[i]['name'], 'x', args[i]['type'], 'x', dir(args[i]), 'y', args[i].get('default'), 'z', args[i].get('defaltValue'), args[i].keys()

        if args[i]['name'].replace(' ', '') == '':
          args[i]['name'] = 'arg' + str(i+1)
        elif args[i]['name'] == '&':
          args[i]['name'] = 'arg' + str(i+1)
          args[i]['type'] += '&'

        #print 'c1', parents.keys()
        if args[i]['type'][-1] == '&':
          sname = args[i]['type'][:-1]
          if sname[-1] == ' ': sname = sname[:-1]
          if sname in parents:
            args[i]['type'] = parents[sname] + '::' + sname + '&'
          elif sname.replace('const ', '') in parents:
            sname = sname.replace('const ', '')
            args[i]['type'] = 'const ' + parents[sname] + '::' + sname + '&'
        #print 'POST arggggggg', classname, 'x', mname, 'x', args[i]['name'], 'x', args[i]['type']

    ret = ((classname + ' *') if constructor else method['returns_text'])#.replace('virtual ', '')
    has_return = ret.replace(' ', '') != 'void'
    callprefix = 'new ' if constructor else ('self->' if not static else (classname + '::'))

    '' # mname used in C
    actualmname = classname if constructor else (method.get('truename') or mname)
    if method.get('getter') or method.get('setter'):
      actualmname = actualmname[4:]

    need_self = not constructor and not static

    def typedargs(args):
      return ([] if not need_self else [classname + ' * self']) + map(lambda i: args[i]['type'] + ' arg' + str(i), range(len(args)))
    def justargs(args):
      return map(lambda i: 'arg' + str(i), range(len(args)))
    def justtypes(args): # note: this ignores 'self'
      return map(lambda i: args[i]['type'], range(len(args)))
    def dummyargs(args):
      return ([] if not need_self else ['(%s*)argv[argc]' % classname]) + map(lambda i: '(%s)argv[argc]' % args[i]['type'], range(len(args)))

    fullname = ('emscripten_bind_' + generating_classname + '__' + mname).replace('::', '__')
    generating_classname_suffixed = generating_classname
    mname_suffixed = mname
    count = funcs.setdefault(fullname, 0)
    funcs[fullname] += 1

    # handle overloading
    dupe = False
    if count > 0:
      dupe = True
      suffix = '_' + str(count+1)
      funcs[fullname + suffix] = 0
      fullname += suffix
      mname_suffixed += suffix
      if constructor:
        generating_classname_suffixed += suffix

    # C

    for args in params:
      i = len(args)
      # If we are returning a *copy* of an object, we return instead to a ref of a static held here. This seems the best compromise
      staticize = not constructor and ret.replace(' ', '') != 'void' and method['returns'] in classes and (not method['returns_reference'] and not method['returns_pointer'])
      # Make sure to mark our bindings wrappers in a way that they will not be inlined, eliminated as unneeded, or optimized into other signatures
      gen_c.write('''
%s __attribute__((used, noinline)) %s_p%d(%s) {''' % (ret if not staticize else (ret + '&'), fullname, i,
                    ', '.join(typedargs(args)[:i + (0 if not need_self else 1)])))
      if not staticize:
        gen_c.write('\n')
        if method.get('getter'):
          gen_c.write('''  return self->%s;''' % actualmname)
        elif method.get('setter'):
          gen_c.write('''  self->%s = arg0;''' % actualmname)
        elif method.get('destroyer'):
          gen_c.write('''  delete self;''')
        elif method.get('operator'):
          gen_c.write(method['operator'])
        else: # normal method
          gen_c.write('''  %s%s%s(%s);''' % ('return ' if has_return else '',
                                             callprefix, actualmname, ', '.join(justargs(args)[:i])))
        gen_c.write('\n')
        gen_c.write('}')
      else:
        gen_c.write('\n')
        if method.get('operator'):
          gen_c.write(method['operator'])
        else:
          gen_c.write('''  static %s ret; ret = %s%s(%s);
  return ret;''' % (method['returns'],
        callprefix, actualmname,
        ', '.join(justargs(args)[:i])))
        gen_c.write('\n}')

    # JS

    #print 'zz types:', map(lambda arg: arg['type'], args)

    has_string_convs = False

    calls = ''

    #print 'js loopin', params, '|', len(args)#, args
    for args in params:
      # We can assume that NULL is passed for null pointers, so object arguments can always
      # have .ptr done on them
      justargs_fixed = justargs(args)[:]
      for i in range(len(args)):
        arg = args[i]
        clean = clean_type(arg['type'])
        if clean in classes:
          justargs_fixed[i] += '.ptr'
        elif arg['type'].replace(' ', '').endswith('char*'):
          justargs_fixed[i] = 'ensureString(' + justargs_fixed[i] + ')'
          has_string_convs = True

      i = len(args)
      if args != params[0]:
        calls += '  else '
      if args != params[-1]:
        calls += '  if (arg' + str(i) + ' === undefined)'
      calls += '\n  ' + ('  ' if len(params) > 0 else '')
      if constructor:
        if not dupe:
          calls += '''this.ptr = _%s_p%d(%s);
''' % (fullname, i, ', '.join(justargs_fixed[:i]))
        else:
          calls += '''this.ptr = _%s_p%d(%s);
''' % (fullname, i, ', '.join(justargs_fixed[:i]))
      else:
        return_value = '''_%s_p%d(%s)''' % (fullname, i, ', '.join((['this.ptr'] if need_self else []) + justargs_fixed[:i]))
        print 'zz making return', classname, method['name'], method['returns'], return_value
        if method['returns'] in classes:
          # Generate a wrapper
          calls += '''return wrapPointer(%s, Module['%s']);''' % (return_value, method['returns'].split('::')[-1])
        else:
          # Normal return
          calls += ('return ' if ret != 'void' else '') + return_value + ';'
        calls += '\n'

    if has_string_convs:
      calls = 'var stack = Runtime.stackSave();\ntry {\n' + calls + '} finally { Runtime.stackRestore(stack) }\n';

    print 'Maekin:', classname, generating_classname, mname, mname_suffixed
    if constructor:
      calls += '''
  %s.prototype.__cache__[this.ptr] = this;
  this.__class__ = %s;''' % (mname_suffixed, mname_suffixed)
      if not dupe:
        js_text = '''
function %s(%s) {
%s
}
%s''' % (mname_suffixed, ', '.join(justargs(args)), calls, generate_wrapping_code(generating_classname_head))
      else:
        js_text = '''
function %s(%s) {
%s
}
%s.prototype = %s.prototype;
''' % (mname_suffixed, ', '.join(justargs(args)), calls, mname_suffixed, classname)

      if export:
        js_text += '''
Module['%s'] = %s;
''' % (mname_suffixed, mname_suffixed)

    else:
      js_text = '''
%s.prototype%s = function(%s) {
%s
}
''' % (generating_classname_head, ('.' + mname_suffixed) if not export else ("['" + mname_suffixed + "']"), ', '.join(justargs(args)), calls)

    js_text = js_text.replace('\n\n', '\n').replace('\n\n', '\n')
    gen_js.write(js_text)

# Main loop

for classname, clazz in parsed.classes.items() + parsed.structs.items():
  if any([name in ignored for name in classname.split('::')]):
    print 'zz ignoring', classname
    continue

  if clazz.get('template_typename'):
    print 'zz ignoring templated base class', classname
    continue

  # Nothing to generate for pure virtual classes XXX actually this is not so. We do need to generate wrappers for returned objects,
  # they are of a concrete class of course, but an known one, so we create a wrapper for an abstract base class.

  possible_prefix = (classname.split('::')[0] + '::') if '::' in classname else ''

  def check_pure_virtual(clazz, progeny):
    #if not clazz.get('inherits'): return False # If no inheritance info, not a class, this is a CppHeaderParser struct
    print 'Checking pure virtual for', clazz['name'], clazz['inherits']
    # If we do not recognize any of the parent classes, assume this is pure virtual - ignore it
    parents = [parent['class'] for parent in clazz['inherits']]
    parents = [parent.split('<')[0] for parent in parents] # remove template stuff
    parents = [parent if parent in classes else possible_prefix + parent for parent in parents]
    if any([not parent in classes for parent in parents]):
      print 'zz Warning: unknown parent class', parents, 'for', classname
      return True
    if any([check_pure_virtual(classes[parent], [clazz] + progeny) for parent in parents]): return True

    def dirtied(mname):
      #print 'zz checking dirtiness for', mname, 'in', progeny
      for progen in progeny:
        for method in progen['methods']:
          if method['name'] == mname and not method['pure_virtual']:
            #print 'zz dirty'
            return True
      #print 'zz not dirtied'
      return False

    for method in clazz['methods']:
      if method['pure_virtual'] and not dirtied(method['name']):
        print 'zz ignoring pure virtual class', classname, 'due to', method['name']
        return True

  clazz['abstract'] = check_pure_virtual(clazz, []) or clazz.get('effectively_abstract')

  print 'zz', classname, 'is abstract?', clazz['abstract']
  #if check_pure_virtual(clazz, []):
  #  continue

  # Add a constructor if none exist
  has_constructor = check_has_constructor(clazz)

  print 'zz', classname, 'has constructor?', has_constructor

  if not has_constructor:
    if not clazz['abstract']:
      print 'zz no constructor for', classname, 'and not abstract, so ignoring'
      continue
  
    #clazz['methods'] = [{
    #  'name': classname,
    #  'parameters': [],
    #  'pure_virtual': False,
    #  'destructor': False,
    #}] + clazz['methods']

  generate_class(classname, classname, clazz)

  # TODO: Add a destructor

# Finish up

gen_c.write('''

}
''')

gen_c.close()
gen_js.close()

